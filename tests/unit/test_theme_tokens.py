"""Tests for the design system's tokens and the stylesheets built from them.

Scope:
    The parts of the visual system that are *claims* rather than taste: the
    contrast ratios the token docstrings assert, that the scales are ordered,
    that the accent is the specified brand colour, and that no colour literal
    escaped into the stylesheets.

    No Qt widget is constructed here - :mod:`omr_scanner.gui.theme.tokens` is
    deliberately Qt-free - so these run in the fast unit suite rather than
    needing a display.

Why contrast is computed and not eyeballed:
    "Secondary text" must not quietly become "unreadable text". The WCAG 2.1
    relative-luminance formula is short enough to implement exactly, so the
    ratios the palette claims are checked rather than asserted in a comment
    that nothing verifies.
"""

from __future__ import annotations

import re

import pytest

from omr_scanner.gui.theme import (
    VARIANT_DESTRUCTIVE,
    VARIANT_PRIMARY,
    VARIANT_PROPERTY,
    Card,
    Color,
    Dashboard,
    FontSize,
    FontWeight,
    Footer,
    Header,
    IconSize,
    Navigator,
    Radius,
    Spacing,
    Stroke,
    application_stylesheet,
)
from omr_scanner.gui.theme.stylesheet import TEMPLATE_DESIGNER_STYLESHEET

BRAND_ACCENT = "#AC1F24"
"""The accent the brief specifies. Written out so a change to
:data:`Color.PRIMARY` has to be deliberate."""

WCAG_AA_NORMAL = 4.5
WCAG_AA_LARGE = 3.0

HEX_COLOUR = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")


def _channel(value: int) -> float:
    """Linearise one 0-255 sRGB channel, per WCAG 2.1."""
    fraction = value / 255.0
    if fraction <= 0.03928:
        return fraction / 12.92
    return ((fraction + 0.055) / 1.055) ** 2.4


def luminance(colour: str) -> float:
    """Relative luminance of a ``#rrggbb`` colour."""
    text = colour.lstrip("#")
    red, green, blue = (int(text[index : index + 2], 16) for index in (0, 2, 4))
    return (
        0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)
    )


def contrast(first: str, second: str) -> float:
    """WCAG contrast ratio between two colours, lighter over darker."""
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


class TestTheAccent:
    def test_the_primary_accent_is_the_specified_brand_colour(self):
        assert Color.PRIMARY.upper() == BRAND_ACCENT

    def test_hover_is_lighter_and_pressed_is_darker(self):
        """The conventional directions.

        Reversing them makes a button feel broken without looking broken.
        """
        assert luminance(Color.PRIMARY_HOVER) > luminance(Color.PRIMARY)
        assert luminance(Color.PRIMARY_PRESSED) < luminance(Color.PRIMARY)

    def test_the_disabled_accent_is_paler_than_the_live_one(self):
        assert luminance(Color.PRIMARY_DISABLED) > luminance(Color.PRIMARY)

    def test_destructive_is_distinguishable_from_primary(self):
        """Destructive is not the accent.

        "Proceed" and "delete permanently" must not be the same colour, or an
        operator learns to click the accent without reading it.
        """
        assert Color.DESTRUCTIVE != Color.PRIMARY

    def test_the_palette_is_predominantly_neutral(self):
        """The interface should be mostly white and grey with one accent.

        Counted rather than asserted in prose: "do not flood the interface
        with red" is the kind of instruction a palette drifts away from one
        colour at a time.
        """
        accents = {
            Color.PRIMARY,
            Color.PRIMARY_HOVER,
            Color.PRIMARY_PRESSED,
            Color.PRIMARY_DISABLED,
            Color.PRIMARY_SOFT,
            Color.FOCUS,
            Color.DESTRUCTIVE,
        }
        neutrals = {
            Color.SURFACE,
            Color.SURFACE_SUNKEN,
            Color.SURFACE_MUTED,
            Color.SURFACE_HOVER,
            Color.SURFACE_PRESSED,
            Color.SURFACE_DISABLED,
            Color.BORDER,
            Color.BORDER_STRONG,
            Color.BORDER_DASHED,
            Color.TEXT_PRIMARY,
            Color.TEXT_SECONDARY,
            Color.TEXT_TERTIARY,
            Color.TEXT_DISABLED,
        }
        assert len(neutrals) > len(accents)


class TestContrast:
    @pytest.mark.parametrize(
        ("name", "foreground", "background"),
        [
            ("primary text on surface", Color.TEXT_PRIMARY, Color.SURFACE),
            ("primary text on sunken", Color.TEXT_PRIMARY, Color.SURFACE_SUNKEN),
            ("primary text on muted", Color.TEXT_PRIMARY, Color.SURFACE_MUTED),
            ("secondary text on surface", Color.TEXT_SECONDARY, Color.SURFACE),
            ("secondary text on sunken", Color.TEXT_SECONDARY, Color.SURFACE_SUNKEN),
            ("secondary text on muted", Color.TEXT_SECONDARY, Color.SURFACE_MUTED),
            ("tertiary text on surface", Color.TEXT_TERTIARY, Color.SURFACE),
            ("tertiary text on sunken", Color.TEXT_TERTIARY, Color.SURFACE_SUNKEN),
            ("white on the accent", Color.TEXT_ON_PRIMARY, Color.PRIMARY),
            ("white on pressed accent", Color.TEXT_ON_PRIMARY, Color.PRIMARY_PRESSED),
            ("charcoal on the soft accent", Color.TEXT_PRIMARY, Color.PRIMARY_SOFT),
        ],
    )
    def test_readable_text_meets_wcag_aa(
        self, name: str, foreground: str, background: str
    ):
        ratio = contrast(foreground, background)
        assert ratio >= WCAG_AA_NORMAL, f"{name}: {ratio:.2f}:1"

    def test_the_accent_on_the_soft_accent_is_legible(self):
        """The Getting Started icon plate: accent glyph on its own pale tint."""
        assert contrast(Color.PRIMARY, Color.PRIMARY_SOFT) >= WCAG_AA_LARGE

    @pytest.mark.parametrize(
        ("name", "colour"),
        [
            ("ready", Color.STATUS_READY),
            ("busy", Color.STATUS_BUSY),
            ("error", Color.STATUS_ERROR),
            ("idle", Color.STATUS_IDLE),
        ],
    )
    def test_the_status_dots_are_visible_on_the_footer(self, name: str, colour: str):
        """The dots must be seen, even though the word carries the meaning.

        They are redundant accents on text, so the large-text threshold is
        the right bar.
        """
        assert contrast(colour, Color.SURFACE_MUTED) >= WCAG_AA_LARGE, name

    def test_disabled_text_stays_legible(self):
        """A disabled control must be readable to be understood.

        Held to the large-text threshold rather than normal-text: it is
        deliberately subdued, and the disabled state is never carried by this
        contrast alone - the control does not respond, is skipped by the focus
        chain, and explains itself in its tooltip.
        """
        assert contrast(Color.TEXT_DISABLED, Color.SURFACE_DISABLED) >= WCAG_AA_LARGE

    def test_the_focus_ring_is_visible_against_both_grounds(self):
        """It has to read on a white surface and on the accent it outlines."""
        assert contrast(Color.FOCUS, Color.SURFACE) >= WCAG_AA_LARGE
        assert contrast(Color.FOCUS_HALO, Color.PRIMARY) >= WCAG_AA_LARGE

    def test_borders_are_visible_without_shouting(self):
        assert contrast(Color.BORDER, Color.SURFACE) > 1.0
        assert luminance(Color.BORDER_STRONG) < luminance(Color.BORDER)


class TestScalesAreOrdered:
    def test_the_spacing_scale_increases(self):
        scale = [
            Spacing.XXS,
            Spacing.XS,
            Spacing.SM,
            Spacing.MD,
            Spacing.LG,
            Spacing.XL,
            Spacing.XXL,
        ]
        assert scale == sorted(scale)
        assert len(set(scale)) == len(scale)

    def test_the_radius_scale_increases(self):
        scale = [Radius.NONE, Radius.SM, Radius.MD, Radius.LG]
        assert scale == sorted(scale)

    def test_the_corner_radii_stay_restrained(self):
        """Examination software, not a consumer dashboard."""
        assert Radius.LG <= 10

    def test_the_icon_sizes_increase(self):
        scale = [IconSize.SM, IconSize.MD, IconSize.LG, IconSize.XL]
        assert scale == sorted(scale)

    def test_the_focus_ring_is_thicker_than_a_border(self):
        assert Stroke.FOCUS_RING > Stroke.BORDER

    def test_the_font_weights_increase(self):
        scale = [
            FontWeight.NORMAL,
            FontWeight.MEDIUM,
            FontWeight.SEMIBOLD,
            FontWeight.BOLD,
        ]
        assert scale == sorted(scale)


class TestTypeSizesAreDeltas:
    def test_the_page_title_is_larger_and_secondary_text_smaller(self):
        assert FontSize.PAGE_TITLE > 0
        assert FontSize.SECONDARY < 0
        assert FontSize.BODY == 0

    def test_there_is_a_readable_floor(self):
        """There is a readable floor.

        A negative delta on a small platform font must not land below a size
        that is still text.
        """
        assert FontSize.MIN_POINT_SIZE >= 7.0

    def test_the_deltas_are_small_enough_to_stay_deltas(self):
        """The deltas stay deltas.

        A delta of twenty would be an absolute size wearing a disguise, and
        would stop respecting the platform font it is added to.
        """
        for delta in (
            FontSize.PAGE_TITLE,
            FontSize.SECTION_TITLE,
            FontSize.SECONDARY,
            FontSize.FOOTER,
            FontSize.TAGLINE,
        ):
            assert abs(delta) <= 8


class TestNavigatorMetrics:
    def test_the_padding_clears_the_arrow(self):
        """The one relationship that matters in the chevron's geometry.

        The arrow eats into the step's box at both ends, so padding that did
        not clear it would let the label collide with the chevron's point.
        """
        assert Navigator.STEP_H_PADDING > 0
        assert Navigator.ARROW_DEPTH > 0
        assert Navigator.STEP_H_PADDING >= Navigator.ARROW_DEPTH * 0.5

    def test_the_seam_is_smaller_than_the_arrow_it_sits_in(self):
        """The seam fits inside the arrow it sits in.

        Otherwise consecutive chevrons would not overlap at all, and the row
        would read as separate tiles.
        """
        assert 0 < Navigator.STEP_GAP < Navigator.ARROW_DEPTH
        assert Navigator.STEP_GAP < Navigator.ARROW_DEPTH_COMPACT

    def test_the_compact_metrics_are_smaller_but_not_absent(self):
        assert Navigator.STEP_HEIGHT_COMPACT < Navigator.STEP_HEIGHT
        assert Navigator.ARROW_DEPTH_COMPACT < Navigator.ARROW_DEPTH
        assert Navigator.STEP_HEIGHT_COMPACT > 0

    def test_a_step_is_tall_enough_to_click_comfortably(self):
        assert Navigator.STEP_HEIGHT >= 32
        assert Navigator.STEP_HEIGHT_COMPACT >= 28

    def test_there_is_a_minimum_label_allowance(self):
        assert Navigator.MIN_LABEL_WIDTH > 0


class TestShellMetrics:
    def test_the_header_is_compact(self):
        """It replaces the menu bar; it must not become a band of its own."""
        assert Header.HEIGHT <= 56

    def test_the_menu_button_is_a_comfortable_target(self):
        assert Header.MENU_BUTTON_SIZE >= 28
        assert Header.MENU_BUTTON_SIZE <= Header.HEIGHT

    def test_the_logo_fits_inside_the_header(self):
        assert Header.LOGO_HEIGHT < Header.HEIGHT

    def test_the_footer_is_compact(self):
        assert Footer.HEIGHT <= 36

    def test_the_page_margin_is_smaller_than_it_was(self):
        """The reclaimed width becomes content, not margin.

        That is what removing the sidebar was for.
        """
        assert Spacing.PAGE_MARGIN < 24
        assert Spacing.PAGE_MARGIN_COMPACT < Spacing.PAGE_MARGIN

    def test_the_card_padding_is_consistent_with_the_spacing_scale(self):
        assert Card.PADDING in {
            Spacing.MD,
            Spacing.LG,
            Spacing.XL,
        }


class TestDashboardProportions:
    def test_the_main_column_gets_more_stretch_than_the_information_column(self):
        assert Dashboard.MAIN_STRETCH > Dashboard.SIDE_STRETCH

    def test_the_information_column_is_bounded_both_ways(self):
        assert 0 < Dashboard.SIDE_MIN_WIDTH < Dashboard.SIDE_MAX_WIDTH

    def test_the_information_column_is_wide_enough_to_read(self):
        assert Dashboard.SIDE_MIN_WIDTH >= 240

    def test_the_stacking_threshold_is_the_sum_of_the_two_minimums(self):
        """A content measurement, not a screen-resolution breakpoint."""
        threshold = (
            Dashboard.MAIN_MIN_WIDTH + Dashboard.SIDE_MIN_WIDTH + Dashboard.COLUMN_GAP
        )
        assert threshold > Dashboard.MAIN_MIN_WIDTH
        assert threshold > Dashboard.SIDE_MIN_WIDTH


class TestStylesheets:
    def test_the_application_stylesheet_uses_the_accent(self):
        assert Color.PRIMARY in application_stylesheet()

    def test_every_colour_in_the_stylesheet_comes_from_the_palette(self):
        """No hex literal may be typed into a stylesheet.

        The whole point of the token module is that a palette change is one
        edit. A literal here would silently opt out of that, and would be
        invisible in review.
        """
        palette = {
            value.upper()
            for name, value in vars(Color).items()
            if not name.startswith("_") and isinstance(value, str)
        }
        for sheet in (application_stylesheet(), TEMPLATE_DESIGNER_STYLESHEET):
            found = {match.upper() for match in HEX_COLOUR.findall(sheet)}
            assert found <= palette, found - palette

    def test_the_button_variants_are_selectable(self):
        sheet = application_stylesheet()
        assert f'[{VARIANT_PROPERTY}="{VARIANT_PRIMARY}"]' in sheet
        assert f'[{VARIANT_PROPERTY}="{VARIANT_DESTRUCTIVE}"]' in sheet

    def test_the_stylesheet_styles_the_dialogs_the_application_never_builds(self):
        """Qt-constructed dialogs are covered.

        `QMessageBox` and friends are built by Qt, so the only way to reach
        them is a stylesheet on the application.
        """
        sheet = application_stylesheet()
        for selector in ("QDialog", "QPushButton", "QLabel", "QMenu", "QToolTip"):
            assert selector in sheet

    def test_no_text_bearing_control_has_a_fixed_height(self):
        """Minimums are fine; a fixed height on a text container is not.

        A stylesheet that pins a control's height fights Qt's layout system
        and clips the label under Windows text scaling - which is exactly the
        scaling this design system is supposed to survive. Checked per rule
        rather than across the whole sheet, because a scrollbar's thickness
        and a divider's hairline are fixed heights that hold no text and are
        entirely correct.
        """
        sheet = application_stylesheet()
        assert "min-height" in sheet

        text_bearing = (
            "QPushButton",
            "QToolButton",
            "QLineEdit",
            "QComboBox",
            "QSpinBox",
            "QDoubleSpinBox",
            "QPlainTextEdit",
            "QTextEdit",
            "QTabBar::tab",
            "QProgressBar",
            "QLabel",
            "QMenu::item",
            "QHeaderView::section",
        )
        offenders: list[str] = []
        for block in re.finditer(r"([^{}]+)\{([^}]*)\}", sheet):
            selector, body = block.group(1).strip(), block.group(2)
            if not any(name in selector for name in text_bearing):
                continue
            if re.search(r"(?<!min-)(?<!max-)height:\s*\d", body):
                offenders.append(selector)
        assert offenders == [], offenders

    def test_the_controls_declare_a_minimum_height_instead(self):
        """So they grow with the font rather than clipping it."""
        sheet = application_stylesheet()
        assert re.search(r"QPushButton\s*\{[^}]*min-height", sheet)
        assert re.search(r"QLineEdit[^{]*\{[^}]*min-height", sheet)

    def test_the_editor_toolbar_sheet_still_covers_every_state(self):
        """Unchanged in purpose: the readability fix it was written for.

        Each interaction state still has an explicit colour, which is the
        whole reason that stylesheet exists - it was added because the native
        style's disabled toolbar controls were unreadable.
        """
        for state in (":hover", ":pressed", ":checked", ":disabled"):
            assert state in TEMPLATE_DESIGNER_STYLESHEET

    def test_the_editor_toolbar_sheet_is_still_scoped_to_controls(self):
        """It is applied per page, so it must not restyle whole containers."""
        assert "QMainWindow" not in TEMPLATE_DESIGNER_STYLESHEET
        assert "QDialog" not in TEMPLATE_DESIGNER_STYLESHEET

    def test_the_stylesheet_is_composed_from_the_tokens_on_each_call(self):
        """Not a frozen copy of them."""
        assert application_stylesheet() == application_stylesheet()
        assert Color.BORDER in application_stylesheet()
