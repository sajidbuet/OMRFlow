"""One-off generator for the application icon's raster derivatives.

Purpose:
    Render ``src/omr_scanner/gui/resources/branding/icon_mark.svg`` (the
    simplified square OMR-bubble mark, not the full two-line wordmark, which
    stops reading as anything recognisable at favicon sizes) into the PNG and
    multi-resolution ``.ico`` files the application icon needs on platforms
    that cannot use an SVG directly (Windows title bars, the taskbar, and any
    future frozen executable's embedded icon resource).

Not part of the installed package or the runtime import graph - run by hand
whenever ``icon_mark.svg`` changes, and its output (``icon.ico``,
``icon_256.png``) is committed to the repository like any other asset, the
same way the bundled Lucide toolbar icons are committed rather than fetched
at run time.

Usage:
    python scripts/generate_branding_assets.py

Why a hand-written ICO writer instead of Pillow:
    This project has no image-manipulation dependency (OMR image decoding
    goes through ``cv2``/``numpy`` in ``imaging``, never through the GUI
    layer), and PySide6 - already a dependency - can rasterise the SVG at
    every needed size and encode a valid PNG for each one; only the top-level
    ICO container (a short, fully documented binary format: a 6-byte header,
    one 16-byte directory entry per image, followed by the images themselves)
    needs to be assembled, and Qt's own ICO writer plugin was found not to
    produce a valid multi-resolution file when driven from PySide6 (repeated
    ``QImageWriter.write()`` calls silently corrupt the directory). Modern
    Windows (Vista onward) accepts a PNG-compressed image directly inside an
    ICO directory entry, which is what every entry here uses - no separate
    BMP/DIB encoding step is needed.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

BRANDING_DIR = Path(__file__).resolve().parents[1] / "src/omr_scanner/gui/resources/branding"
SOURCE_SVG = BRANDING_DIR / "icon_mark.svg"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
PNG_SIZE = 256


def render_png_bytes(renderer: QSvgRenderer, size: int) -> bytes:
    """Rasterise ``renderer`` into a transparent ``size`` x ``size`` PNG, returned as bytes."""
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    renderer.render(painter, image.rect())
    painter.end()

    buffer = QByteArray()
    device = QBuffer(buffer)
    device.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(device, "PNG"):
        raise RuntimeError(f"Failed to encode a {size}x{size} PNG")
    device.close()
    return bytes(buffer)


def write_ico(path: Path, images: list[tuple[int, bytes]]) -> None:
    """Write a multi-resolution ``.ico`` with PNG-compressed frames.

    Args:
        path: Destination file.
        images: ``(size, png_bytes)`` pairs, one per resolution. ``size`` of
            256 is encoded as ``0`` in the directory entry, per the ICO
            format's convention for "256 or larger".
    """
    header = struct.pack("<HHH", 0, 1, len(images))
    directory = b""
    image_data = b""
    offset = 6 + 16 * len(images)
    for size, png_bytes in images:
        dimension_byte = 0 if size >= 256 else size
        directory += struct.pack(
            "<BBBBHHII",
            dimension_byte,  # width
            dimension_byte,  # height
            0,  # colour palette size (0 = no palette, true colour)
            0,  # reserved
            1,  # colour planes
            32,  # bits per pixel
            len(png_bytes),
            offset,
        )
        image_data += png_bytes
        offset += len(png_bytes)
    path.write_bytes(header + directory + image_data)


def main() -> int:
    """Render `icon_mark.svg` into `icon.ico` and `icon_256.png`."""
    QApplication.instance() or QApplication([])

    renderer = QSvgRenderer(str(SOURCE_SVG))
    if not renderer.isValid():
        print(f"Could not load {SOURCE_SVG}", file=sys.stderr)
        return 1

    images = [(size, render_png_bytes(renderer, size)) for size in ICO_SIZES]

    ico_path = BRANDING_DIR / "icon.ico"
    write_ico(ico_path, images)
    print(f"Wrote {ico_path} ({', '.join(f'{s}x{s}' for s, _ in images)})")

    png_path = BRANDING_DIR / "icon_256.png"
    png_path.write_bytes(render_png_bytes(renderer, PNG_SIZE))
    print(f"Wrote {png_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
