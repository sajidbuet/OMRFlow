"""Generate the animated workflow demonstration shown at the top of README.md.

Purpose:
    Produce ``docs/images/omrflow-workflow-stages.gif`` from the *real*
    application, reproducibly, so the README's first impression can be
    regenerated after a user-interface change instead of being a screen
    recording nobody can reproduce.

Usage:
    python scripts/generate_readme_demo.py
    python scripts/generate_readme_demo.py --output /tmp/preview.gif --fps 10

What it is, and what it is not:
    It drives the genuine `MainWindow` through the genuine workflow - the same
    widgets, stylesheet and pages the application ships - and photographs it
    with ``QWidget.grab()``. It is not a mock-up, and it is not a recording of
    a desktop: ``grab()`` renders the widget itself, so no terminal, editor,
    notification or wallpaper can appear in the result no matter what is on
    screen while it runs.

Why the pointer is drawn rather than captured:
    ``grab()`` deliberately does not composite the system cursor, and a real
    screen recording would bring the whole desktop with it. The pointer here is
    drawn onto each frame at a known position, which also makes its path
    deterministic - it moves where the storyboard says, at the same speed,
    every time. A demonstration where the pointer drifts is a demonstration
    recorded by hand.

Why identical frames are collapsed:
    Most of a user-interface demonstration is holds - a second on a page so it
    can be read. Written out at the frame rate those are dozens of byte-
    identical frames. Collapsing each run into one frame with a longer delay is
    lossless, and is most of the difference between a GIF measured in tens of
    megabytes and one a README can afford.

Safety:
    Everything it touches is temporary. The demonstration project is created
    under a temporary directory and deleted afterwards, and the application is
    pointed at a throwaway configuration file so the machine's real settings
    and recent-project list are neither read nor written. The only names that
    appear are the fictitious ones in :data:`DEMO_PROJECT_NAME` and
    :data:`DEMO_EXAM_NAME`.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from PIL import Image, ImageDraw  # noqa: E402
from PySide6.QtCore import QElapsedTimer, QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from omr_scanner.config import AppConfig  # noqa: E402
from omr_scanner.gui.main_window import MainWindow  # noqa: E402
from omr_scanner.gui.theme import application_stylesheet  # noqa: E402

DEFAULT_OUTPUT = REPOSITORY_ROOT / "docs" / "images" / "omrflow-workflow-stages.gif"

SAMPLE_TEMPLATE = REPOSITORY_ROOT / "examples" / "templates" / "ece_0000_sample.omrt"
SAMPLE_SHEET = REPOSITORY_ROOT / "examples" / "ECE-0000.png"

DEMO_PROJECT_NAME = "OMRFlow Demo Project"
DEMO_EXAM_NAME = "Sample Examination"
"""Fictitious throughout. Nothing real - no candidate, no institution, no
examination - may appear in a file published in the repository."""

DEMO_WORKSPACE = REPOSITORY_ROOT / "test-output" / "readme-demo"
"""Where the demonstration project is created.

Inside the repository's git-ignored scratch directory rather than the system
temporary folder, and that is a privacy decision rather than a tidiness one.
The Project stage displays the open project's *Location*, and the Recent
Projects card repeats it - so on Windows a temporary path would put the
machine's account name into a file published on GitHub. A path under the
checkout names nobody. Deleted after every run either way.
"""

WINDOW_WIDTH = 1400
WINDOW_HEIGHT = 788
"""The window is captured at one size for the whole run and never resized
mid-demonstration: a README animation that jumps between shapes is harder to
read than one that does not, and every frame of a GIF must be the same size
anyway.

Wide enough that the ribbon shows all nine stages at once. Narrower than this
and the ribbon is correct to start scrolling, but a demonstration whose whole
subject is the nine-stage workflow should not open with the ninth stage
half off the edge."""

GIF_WIDTH = 1200
GIF_HEIGHT = 675
"""16:9, and inside the 1100-1400px the README wants. Captured larger than
this and scaled down, so on a high-DPI machine the result is supersampled
rather than merely resized."""

DEFAULT_FPS = 12
MOVE_FRAMES = 9
"""How long the pointer takes to travel between two controls. Long enough to
follow, short enough not to be the thing you are waiting for."""

CLICK_RING_FRAMES = 3
GIF_COLOURS = 128
"""Palette size. The interface is flat colour and thin text; 128 is enough for
both and materially smaller than 256."""


# ----------------------------------------------------------------------
# The drawn pointer
# ----------------------------------------------------------------------
_ARROW = (
    (0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12),
)
"""A conventional arrow cursor, as a polygon in a 12x20 box."""


def _draw_pointer(image: Image.Image, at: tuple[float, float], click: float) -> None:
    """Draw the pointer at ``at``, with a click ring when ``click`` is in (0, 1].

    White fill with a dark outline, because the pointer has to stay visible
    over a white page, a grey chevron and the accent-red active step alike.
    """
    draw = ImageDraw.Draw(image, "RGBA")
    x, y = at

    if click > 0.0:
        radius = 6 + 18 * (1.0 - click)
        alpha = int(150 * click)
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            outline=(172, 31, 36, alpha),
            width=3,
        )

    polygon = [(x + dx, y + dy) for dx, dy in _ARROW]
    draw.polygon(polygon, fill=(255, 255, 255, 255), outline=(20, 20, 20, 255))
    draw.line([*polygon, polygon[0]], fill=(20, 20, 20, 255), width=2)


def _ease(t: float) -> float:
    """Ease-in-out, so the pointer accelerates and settles instead of sliding."""
    return 3 * t * t - 2 * t * t * t


# ----------------------------------------------------------------------
# The storyboard
# ----------------------------------------------------------------------
@dataclass
class Beat:
    """One step of the demonstration.

    Attributes:
        hold: Frames to stay on this state once any pointer move has finished.
        move_to: A callable returning the widget the pointer should travel to,
            resolved at capture time because the widget may not exist - or may
            not be positioned - until earlier beats have run.
        action: What to do when the pointer arrives. Its frame is marked as a
            click.
        label: Short description, printed while capturing.
    """

    label: str
    hold: int
    move_to: Callable[[MainWindow], QWidget | None] | None = None
    action: Callable[[MainWindow], None] | None = None
    settle: int = field(default=2)


def _step(window: MainWindow, key: str) -> QWidget | None:
    return window.ribbon.step(key)


def _centre_in_window(window: MainWindow, widget: QWidget) -> tuple[float, float]:
    centre = widget.mapTo(window, QPoint(widget.width() // 2, widget.height() // 2))
    return (float(centre.x()), float(centre.y()))


def _storyboard() -> list[Beat]:
    """The demonstration, in order.

    Deliberately short of exhaustive. It answers "what does OMRFlow look like
    and how does its workflow move" - the branded chrome row, the nine-stage
    ribbon, the project opening and appearing in the footer, three
    representative stages, the previous/next arrows, and the density control -
    and then stops.
    """
    return [
        Beat("open on the Project stage", hold=14),
        Beat(
            "create the demonstration project",
            hold=18,
            move_to=lambda w: w._pages["project"].create_button,
            action=_create_demo_project,
        ),
        Beat(
            "Template stage, with a real template open",
            hold=20,
            move_to=lambda w: _step(w, "template"),
            action=_open_template_stage,
        ),
        Beat(
            "next stage: Calibrate",
            hold=10,
            move_to=lambda w: w.chrome.next_button,
            action=lambda w: w.go_to_next_stage(),
        ),
        Beat(
            "next stage: Scan, with a sheet imported",
            hold=22,
            move_to=lambda w: w.chrome.next_button,
            action=_open_scan_stage,
        ),
        Beat(
            "Attendance stage",
            hold=14,
            move_to=lambda w: _step(w, "attendance"),
            action=lambda w: w.show_page("attendance"),
        ),
        Beat(
            "Reports stage",
            hold=18,
            move_to=lambda w: _step(w, "reports"),
            action=lambda w: w.show_page("reports"),
        ),
        Beat(
            "tighten the ribbon with the density control",
            hold=16,
            move_to=lambda w: w.chrome.density_out_button,
            action=lambda w: w.chrome.density_out_button.click(),
        ),
        Beat("rest on the finished shell", hold=10),
    ]


def _create_demo_project(window: MainWindow) -> None:
    shutil.rmtree(DEMO_WORKSPACE, ignore_errors=True)
    DEMO_WORKSPACE.mkdir(parents=True, exist_ok=True)
    window._demo_root = DEMO_WORKSPACE  # type: ignore[attr-defined]
    window.create_project_at(DEMO_WORKSPACE, DEMO_PROJECT_NAME, exam_name=DEMO_EXAM_NAME)


def _open_template_stage(window: MainWindow) -> None:
    """Open the bundled sample template and fit it to the canvas.

    Fitting matters: a freshly opened 2480x3508 sheet lands at about 5% zoom,
    which draws the answer sheet as a postage stamp in the middle of an empty
    canvas. The Template stage's whole point is the marked-up sheet, so the
    demonstration shows it at a size where the regions are visible - which is
    also the first thing a real operator does.
    """
    window.edit_template(SAMPLE_TEMPLATE)
    page = window._pages["template"]
    page.canvas.fit_to_window()


def _open_scan_stage(window: MainWindow) -> None:
    """Show the Scan stage with the sample template and one sheet imported.

    The real page, loaded through its own public commands - the same ones the
    Import buttons call - so what the animation shows is what the application
    does, not a dressed-up empty state.
    """
    window.go_to_next_stage()
    scan = window._pages["scan"]
    if not (SAMPLE_TEMPLATE.is_file() and SAMPLE_SHEET.is_file()):
        return
    scan.load_template_from(SAMPLE_TEMPLATE)
    scan.add_scan_paths([SAMPLE_SHEET])

    # Actually recognise the sheet, rather than showing it queued. The preview
    # is of the *rectified* page recognition produces, so an unprocessed scan
    # leaves the canvas blank beside a populated file list - which reads as a
    # broken screenshot. One real sheet through the real pipeline takes a
    # couple of seconds and is the single most informative thing the animation
    # can show: the roll number and answers appear in the panel on the right,
    # read off the sheet on the left.
    finished: list[object] = []
    scan.batch_finished.connect(finished.append)
    try:
        if scan.process_all():
            _await(lambda: bool(finished), timeout_ms=120_000)
    finally:
        scan.batch_finished.disconnect(finished.append)

    # Selecting a row starts a `PreviewWorker`; grabbing before it lands
    # captures an empty canvas.
    scan.select_scan(0)
    _await(lambda: scan.preview.has_page)
    scan.preview.fit_to_window()


def _await(condition: Callable[[], bool], *, timeout_ms: int = 60_000) -> bool:
    """Pump the event loop until ``condition`` holds, or the timeout passes."""
    clock = QElapsedTimer()
    clock.start()
    while not condition():
        QApplication.processEvents()
        if clock.elapsed() > timeout_ms:
            return False
    QApplication.processEvents()
    return True


# ----------------------------------------------------------------------
# Capture
# ----------------------------------------------------------------------
def _settle(app: QApplication, rounds: int = 3) -> None:
    for _ in range(rounds):
        app.processEvents()


def _frame(window: MainWindow) -> Image.Image:
    """One frame: the window itself, scaled to the GIF's size."""
    pixmap = window.grab()
    image = pixmap.toImage().convertToFormat(
        pixmap.toImage().Format.Format_RGBA8888
    )
    raw = image.constBits().tobytes()
    frame = Image.frombytes(
        "RGBA", (image.width(), image.height()), raw, "raw", "RGBA", image.bytesPerLine()
    )
    return frame.convert("RGB").resize((GIF_WIDTH, GIF_HEIGHT), Image.LANCZOS)


def _scaled(window: MainWindow, point: tuple[float, float]) -> tuple[float, float]:
    """A window-space point in GIF-space."""
    return (
        point[0] * GIF_WIDTH / window.width(),
        point[1] * GIF_HEIGHT / window.height(),
    )


def _capture(app: QApplication, window: MainWindow) -> Iterator[Image.Image]:
    """Walk the storyboard, yielding one image per frame."""
    pointer = (WINDOW_WIDTH * 0.62, WINDOW_HEIGHT * 0.80)

    for beat in _storyboard():
        print(f"  {beat.label}")
        target = beat.move_to(window) if beat.move_to else None
        if target is not None:
            destination = _centre_in_window(window, target)
            start = pointer
            for index in range(1, MOVE_FRAMES + 1):
                progress = _ease(index / MOVE_FRAMES)
                pointer = (
                    start[0] + (destination[0] - start[0]) * progress,
                    start[1] + (destination[1] - start[1]) * progress,
                )
                frame = _frame(window)
                _draw_pointer(frame, _scaled(window, pointer), click=0.0)
                yield frame
            pointer = destination

        if beat.action is not None:
            beat.action(window)
            _settle(app)
            for index in range(CLICK_RING_FRAMES):
                frame = _frame(window)
                _draw_pointer(
                    frame,
                    _scaled(window, pointer),
                    click=1.0 - index / CLICK_RING_FRAMES,
                )
                yield frame

        _settle(app)
        for _ in range(beat.hold):
            frame = _frame(window)
            _draw_pointer(frame, _scaled(window, pointer), click=0.0)
            yield frame


def _collapse(frames: list[Image.Image], frame_ms: int) -> tuple[list[Image.Image], list[int]]:
    """Merge runs of identical frames into one frame with a longer delay.

    Lossless, and the single biggest saving available: a two-second hold is one
    frame with a 2,000 ms delay rather than twenty-four copies of it.
    """
    kept: list[Image.Image] = []
    delays: list[int] = []
    for frame in frames:
        if kept and frame.tobytes() == kept[-1].tobytes():
            delays[-1] += frame_ms
            continue
        kept.append(frame)
        delays.append(frame_ms)
    return kept, delays


def build(output: Path, fps: int) -> Path:
    """Capture the demonstration and write ``output``. Returns the path."""
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication([])
    app.setStyleSheet(application_stylesheet())

    config_dir = Path(tempfile.mkdtemp(prefix="omrflow_demo_config_"))
    window = MainWindow(config=AppConfig(), config_path=config_dir / "config.json")
    window.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
    window.show()
    _settle(app, rounds=6)

    demo_root: Path | None = None
    try:
        frames = list(_capture(app, window))
        demo_root = getattr(window, "_demo_root", None)
    finally:
        window.close_project()
        window.close()
        _settle(app)
        shutil.rmtree(config_dir, ignore_errors=True)
        if demo_root is not None:
            shutil.rmtree(demo_root, ignore_errors=True)

    frame_ms = round(1000 / fps)
    kept, delays = _collapse(frames, frame_ms)
    print(f"  {len(frames)} frames captured, {len(kept)} after collapsing holds")

    quantised = _quantise(kept)
    output.parent.mkdir(parents=True, exist_ok=True)
    quantised[0].save(
        output,
        save_all=True,
        append_images=quantised[1:],
        duration=delays,
        loop=0,
        optimize=True,
        # "Leave the previous frame in place", which is what lets the encoder
        # store only the rectangle that changed. Most of this animation is a
        # static shell with one panel changing, so the saving is large - but it
        # only works because every frame shares the one palette built below.
        disposal=1,
    )
    return output


def _quantise(frames: list[Image.Image]) -> list[Image.Image]:
    """Map every frame onto a single shared palette.

    Quantising each frame independently gives each its own local colour table,
    which both costs bytes per frame and defeats inter-frame differencing -
    the encoder cannot say "only this rectangle changed" when the palettes
    underneath disagree. One palette derived from the whole animation fixes
    both. Built from a strip of evenly spaced frames rather than from all of
    them, because the colours are the interface's and a dozen samples already
    contain every one of them.
    """
    sample_indices = range(0, len(frames), max(1, len(frames) // 12))
    samples = [frames[index] for index in sample_indices]
    strip = Image.new("RGB", (samples[0].width, samples[0].height * len(samples)))
    for row, sample in enumerate(samples):
        strip.paste(sample, (0, row * sample.height))
    master = strip.quantize(colors=GIF_COLOURS, method=Image.MEDIANCUT)
    return [frame.quantize(palette=master, dither=Image.NONE) for frame in frames]


def main(argv: list[str] | None = None) -> int:
    """Generate the README demonstration and report what was written."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    args = parser.parse_args(argv)

    print(f"Capturing the OMRFlow workflow demonstration at {args.fps} fps")
    written = build(args.output, args.fps)
    size_mb = written.stat().st_size / (1024 * 1024)
    print(f"\nWrote {written} ({GIF_WIDTH}x{GIF_HEIGHT}, {size_mb:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
