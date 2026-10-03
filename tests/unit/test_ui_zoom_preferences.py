"""Tests for the interface zoom's arithmetic, preference and stylesheet.

Scope:
    Everything about the interface zoom that can be decided without a display:
    the :class:`~omr_scanner.gui.theme.UiScale` rounding rules, the stored
    ``ui_zoom_percent`` preference, and the scaled application stylesheet.
    The live behaviour - widgets, fonts, menus - is in
    ``tests/gui/test_ui_zoom.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from omr_scanner.config import (
    DEFAULT_UI_ZOOM_PERCENT,
    MAX_UI_ZOOM_PERCENT,
    MIN_UI_ZOOM_PERCENT,
    UI_ZOOM_STEP_PERCENT,
    AppConfig,
    load_app_config,
    save_app_config,
)
from omr_scanner.gui.theme import Chrome, FontSize, Navigator, Spacing, UiScale
from omr_scanner.gui.theme.stylesheet import (
    SCALED_STYLESHEETS,
    TEMPLATE_DESIGNER_STYLESHEET,
    application_stylesheet,
    template_designer_stylesheet,
)


# ----------------------------------------------------------------------
# The stored preference
# ----------------------------------------------------------------------
class TestPreference:
    def test_the_default_is_100_percent(self):
        assert AppConfig().ui_zoom_percent == 100 == DEFAULT_UI_ZOOM_PERCENT

    def test_the_range_and_step_are_the_documented_ones(self):
        assert (MIN_UI_ZOOM_PERCENT, MAX_UI_ZOOM_PERCENT, UI_ZOOM_STEP_PERCENT) == (80, 200, 10)

    def test_the_zoom_is_saved_to_the_user_configuration(self, tmp_path: Path):
        path = tmp_path / "omrflow.config.json"
        save_app_config(AppConfig().with_ui_zoom_percent(130), path)
        assert json.loads(path.read_text(encoding="utf-8"))["ui_zoom_percent"] == 130
        assert load_app_config(path, strict=True).ui_zoom_percent == 130

    def test_a_file_written_before_the_zoom_existed_loads_at_100(self, tmp_path: Path):
        """An additive field with a safe default: no config_version bump."""
        path = tmp_path / "omrflow.config.json"
        payload = AppConfig().model_dump(mode="json")
        del payload["ui_zoom_percent"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = load_app_config(path, strict=True)
        assert loaded.ui_zoom_percent == 100
        assert loaded.config_version == payload["config_version"]

    @pytest.mark.parametrize(("stored", "expected"), [(250, 200), (10, 80), (-5, 80), (200, 200)])
    def test_a_stored_value_outside_the_range_is_clamped_not_rejected(
        self, tmp_path: Path, stored: int, expected: int
    ):
        """A rejected value would reset every other preference with it."""
        path = tmp_path / "omrflow.config.json"
        payload = AppConfig().with_recent_project(tmp_path).model_dump(mode="json")
        payload["ui_zoom_percent"] = stored
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = load_app_config(path, strict=True)
        assert loaded.ui_zoom_percent == expected
        assert loaded.recent_projects == (tmp_path.resolve(),)

    def test_with_ui_zoom_percent_clamps_and_leaves_the_receiver_alone(self):
        original = AppConfig()
        assert original.with_ui_zoom_percent(999).ui_zoom_percent == MAX_UI_ZOOM_PERCENT
        assert original.with_ui_zoom_percent(1).ui_zoom_percent == MIN_UI_ZOOM_PERCENT
        assert original.with_ui_zoom_percent(120).ui_zoom_percent == 120
        assert original.ui_zoom_percent == 100

    def test_the_zoom_and_the_ribbon_density_are_independent(self):
        config = AppConfig().with_ribbon_density(3).with_ui_zoom_percent(150)
        assert (config.ribbon_density, config.ui_zoom_percent) == (3, 150)


# ----------------------------------------------------------------------
# The arithmetic
# ----------------------------------------------------------------------
class TestUiScale:
    def test_100_percent_is_the_identity_on_every_token(self):
        identity = UiScale.of(100)
        for value in (
            Chrome.HEIGHT, Chrome.SMALL_BUTTON_SIZE, Navigator.STEP_HEIGHT,
            Spacing.XXS, Spacing.MD, Spacing.XXL, 1, 0,
        ):
            assert identity.px(value) == value
        assert identity.stroke(1) == 1
        assert identity.point_size(9.0, FontSize.PAGE_TITLE) == 15.0

    def test_lengths_scale_from_the_canonical_value(self):
        assert UiScale.of(150).px(Chrome.HEIGHT) == 69
        assert UiScale.of(200).px(Chrome.HEIGHT) == 92
        assert UiScale.of(80).px(Chrome.HEIGHT) == 37

    def test_successive_zooms_do_not_compound(self):
        """110% then 120% is 120%, not 1.10 x 1.20 - nothing remembers 110%."""
        assert UiScale.of(120).px(Spacing.XL) == UiScale(120).px(Spacing.XL) == 29
        assert UiScale.of(120).px(UiScale.of(110).px(Spacing.XL)) != UiScale.of(120).px(Spacing.XL)

    def test_100_150_100_returns_exactly_to_the_canonical_values(self):
        for value in range(0, 200):
            UiScale.of(150).px(value)
            assert UiScale.of(100).px(value) == value

    def test_a_non_zero_length_never_rounds_to_zero(self):
        assert UiScale.of(80).px(1) == 1
        assert UiScale(10).px(1) == 1
        assert UiScale.of(80).px(0) == 0

    def test_a_hairline_never_disappears_and_only_thickens_when_doubled(self):
        assert [UiScale.of(p).stroke(1) for p in (80, 100, 150, 190, 200)] == [1, 1, 1, 1, 2]

    def test_font_deltas_scale_with_the_text_they_are_relative_to(self):
        """(base + delta) x factor, not (base x factor) + delta."""
        assert UiScale.of(150).point_size(9.0, FontSize.PAGE_TITLE) == pytest.approx(22.5)
        assert UiScale.of(150).point_size(9.0, FontSize.FOOTER) == pytest.approx(12.0)

    def test_the_minimum_font_floor_applies_before_the_zoom(self):
        assert UiScale.of(100).point_size(7.0, -1) == FontSize.MIN_POINT_SIZE
        assert UiScale.of(200).point_size(7.0, -1) == FontSize.MIN_POINT_SIZE * 2

    def test_of_clamps_to_the_supported_range(self):
        assert UiScale.of(500).percent == MAX_UI_ZOOM_PERCENT
        assert UiScale.of(5).percent == MIN_UI_ZOOM_PERCENT


# ----------------------------------------------------------------------
# The stylesheets
# ----------------------------------------------------------------------
def _px(sheet: str, selector: str, prop: str) -> int:
    """The first ``prop: <n>px`` inside the rule for ``selector``."""
    block = sheet[sheet.index(selector + " {"):]
    block = block[: block.index("}")]
    match = re.search(rf"\b{prop}:\s*(\d+)px", block)
    assert match, (selector, prop)
    return int(match.group(1))


class TestScaledStylesheet:
    def test_100_percent_is_the_canonical_stylesheet(self):
        assert application_stylesheet(UiScale.of(100)) == application_stylesheet()
        assert template_designer_stylesheet() == TEMPLATE_DESIGNER_STYLESHEET

    @pytest.mark.parametrize(
        ("selector", "prop"),
        [
            ("QPushButton", "min-height"),
            ("QScrollBar:vertical", "width"),
            ("QComboBox::drop-down", "width"),
            ("QProgressBar", "min-height"),
            ("QAbstractSpinBox::up-button, QAbstractSpinBox::down-button", "width"),
        ],
    )
    def test_geometry_grows_and_shrinks_with_the_zoom(self, selector: str, prop: str):
        small = _px(application_stylesheet(UiScale.of(80)), selector, prop)
        base = _px(application_stylesheet(UiScale.of(100)), selector, prop)
        large = _px(application_stylesheet(UiScale.of(150)), selector, prop)
        assert small < base < large

    def test_borders_stay_hairlines_at_150_percent(self):
        sheet = application_stylesheet(UiScale.of(150))
        assert "border: 1px solid" in sheet
        assert "border: 2px solid" not in sheet.split("QPushButton:focus")[0]

    def test_colours_do_not_change_with_the_zoom(self):
        def colours(sheet: str) -> list[str]:
            return re.findall(r"#[0-9A-Fa-f]{6}", sheet)

        assert colours(application_stylesheet(UiScale.of(200))) == colours(application_stylesheet())

    def test_every_page_scoped_sheet_is_registered_and_scales(self):
        for name, compose in SCALED_STYLESHEETS.items():
            assert compose(UiScale.of(100)) != compose(UiScale.of(200)), name
