"""Tests for `omr_scanner.gui.icons.load_icon`."""

from __future__ import annotations

import pytest

from omr_scanner.gui.icons import load_icon

pytestmark = pytest.mark.gui


class TestLoadIcon:
    def test_a_bundled_icon_loads_as_a_non_null_qicon(self):
        icon = load_icon("save")
        assert not icon.isNull()

    def test_the_same_name_returns_the_cached_instance(self):
        first = load_icon("save")
        second = load_icon("save")
        assert first is second

    def test_different_names_return_different_icons(self):
        assert load_icon("save") is not load_icon("folder-open")

    def test_an_unknown_icon_name_raises(self):
        with pytest.raises(FileNotFoundError, match="does-not-exist"):
            load_icon("does-not-exist")

    @pytest.mark.parametrize(
        "name",
        [
            "file-plus", "folder-open", "save", "save-all", "undo-2", "redo-2",
            "scan-line", "check-check", "id-card", "list-checks", "circle-dot",
            "copy-plus", "align-horizontal-distribute-center",
            "square-dashed", "file-text", "pencil", "rotate-ccw", "circle-check",
            "zoom-in", "zoom-out", "maximize", "scan", "grid-3x3", "copy", "trash",
        ],
    )
    def test_every_icon_used_by_the_template_designer_is_bundled(self, name: str):
        # One entry per row of
        # `omr_scanner/gui/resources/icons/lucide/README.md`'s "Icons in use"
        # table - if this list and that table ever disagree, one of them is
        # wrong.
        assert not load_icon(name).isNull()
