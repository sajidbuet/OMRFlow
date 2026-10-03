"""Capture the application at several interface zooms and window sizes.

Purpose:
    Evidence for the global interface zoom: screenshots of the shell and the
    denser stages at 80/100/120/150/200 % on a 1366x768 and an 1100x680
    window, plus a JSON report of the geometry checks a screenshot cannot be
    trusted for - clipped buttons, controls outside the window, the ribbon and
    footer layouts, and whether the sheet zoom survived.

Run from the repository root, on the native platform (not offscreen - the
point is the real font metrics):

    python .claude/skills/qtguitesting/scripts/capture_ui_zoom.py

Output: ``test-output/gui/zoom/`` (git-ignored).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QWidget

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "src"))

from omr_scanner.config import AppConfig  # noqa: E402
from omr_scanner.gui.application import configure_application  # noqa: E402
from omr_scanner.gui.main_window import MainWindow  # noqa: E402
from omr_scanner.gui.settings_dialog import SettingsDialog  # noqa: E402

OUT = REPO / "test-output" / "gui" / "zoom"
SIZES = [(1366, 768), (1100, 680)]
ZOOMS = [80, 100, 120, 150, 200]
PAGES = ["project", "template", "resolve", "attendance", "results", "reports"]
SAMPLE_TEMPLATE = REPO / "Sample-Project" / "1.Template" / "BUET100q.omrt"


def settle(app: QApplication, rounds: int = 6) -> None:
    """Let Qt run its pending layout and paint events."""
    for _ in range(rounds):
        app.processEvents()


def clipped_controls(root: QWidget) -> list[str]:
    """Visible buttons/labels with less room than their own minimum."""
    problems = []
    for widget in root.findChildren(QWidget):
        if not widget.isVisible() or not isinstance(widget, (QAbstractButton, QLabel)):
            continue
        if isinstance(widget, QLabel) and (widget.wordWrap() or not widget.text()):
            continue
        hint = widget.minimumSizeHint()
        if hint.width() <= 0:
            continue
        if widget.width() + 1 < hint.width() or widget.height() + 1 < hint.height():
            name = widget.objectName() or getattr(widget, "text", lambda: "")()
            problems.append(f"{type(widget).__name__}:{name!r} {widget.width()}x{widget.height()}"
                            f" < {hint.width()}x{hint.height()}")
    return problems


def main() -> int:
    """Capture every size and zoom; write the screenshots and the report."""
    app = QApplication(sys.argv)
    configure_application(app)
    work = Path(tempfile.mkdtemp(prefix="omr_zoom_"))
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    window = MainWindow(config=AppConfig(), config_path=work / "config.json")
    window.show()
    settle(app)
    workspace = work / "workspace"
    workspace.mkdir()
    window.create_project_at(workspace, "Zoom Validation Exam")
    template_ok = SAMPLE_TEMPLATE.is_file() and window.edit_template(SAMPLE_TEMPLATE)
    settle(app)
    canvas = window._pages["template"].canvas
    canvas._apply_zoom(1.25)

    report: dict[str, object] = {"template_loaded": bool(template_ok), "runs": []}
    runs: list[dict[str, object]] = []
    for width, height in SIZES:
        window.showNormal()
        window.resize(width, height)
        settle(app)
        for percent in ZOOMS:
            window.set_interface_zoom(percent)
            settle(app)
            chrome = window.chrome
            close = chrome.close_button
            close_right = close.mapTo(window, close.rect().bottomRight()).x()
            entry: dict[str, object] = {
                "size": f"{width}x{height}",
                "zoom": percent,
                "window": [window.width(), window.height()],
                "window_min_hint": list(window.minimumSizeHint().toTuple()),
                "chrome_height": chrome.height(),
                "ribbon_mode": window.ribbon.mode.value,
                "elided_steps": [s.display_text for s in window.ribbon.steps
                                 if s.isVisible() and s.is_label_elided],
                "close_button_inside_window": close_right <= window.width(),
                "footer_tier": window.footer.tier.value if window.footer.tier else None,
                "footer_height": window.footer.height(),
                "pages": {},
            }
            tag = f"{width}x{height}"
            folder = OUT / tag
            folder.mkdir(exist_ok=True)
            for key in PAGES:
                window.show_page(key)
                settle(app)
                page = window._pages[key]
                stack = window.stack
                pages = entry["pages"]
                assert isinstance(pages, dict)
                pages[key] = {
                    "min_hint": list(page.minimumSizeHint().toTuple()),
                    "viewport": list(stack.viewport().size().toTuple()),
                    "h_scroll": stack.horizontalScrollBar().isVisible(),
                    "v_scroll": stack.verticalScrollBar().isVisible(),
                    "clipped": clipped_controls(page),
                }
                window.grab().save(str(folder / f"{percent:03d}_{key}.png"))
            entry["template_canvas_zoom"] = round(canvas.zoom, 4)
            chrome.grab().save(str(folder / f"{percent:03d}_chrome.png"))
            window.footer.grab().save(str(folder / f"{percent:03d}_footer.png"))
            dialog = SettingsDialog(window.config, window, cpu_count=8)
            dialog.show()
            settle(app)
            entry["settings_dialog_font_pt"] = round(dialog.font().pointSizeF(), 2)
            entry["settings_dialog_clipped"] = clipped_controls(dialog)
            dialog.grab().save(str(folder / f"{percent:03d}_settings_dialog.png"))
            dialog.close()
            window.application_menu.popup(chrome.menu_button.mapToGlobal(
                chrome.menu_button.rect().bottomLeft()))
            settle(app)
            window.application_menu.grab().save(str(folder / f"{percent:03d}_menu.png"))
            view_menu = next(a.menu() for a in window.application_menu.actions()
                             if a.menu() is not None and a.menu().title() == "&View")
            view_menu.popup(window.application_menu.geometry().topRight())
            settle(app)
            view_menu.grab().save(str(folder / f"{percent:03d}_view_menu.png"))
            view_menu.close()
            window.application_menu.close()
            runs.append(entry)
            print(f"{tag} {percent}%: ribbon={entry['ribbon_mode']} footer={entry['footer_tier']} "
                  f"canvas={entry['template_canvas_zoom']} "
                  f"clipped={sum(len(p['clipped']) for p in entry['pages'].values())}")
    report["runs"] = runs
    window.set_interface_zoom(100)
    session = window.session
    if session is not None:
        window.close_project()
    window.close()
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
