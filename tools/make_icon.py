"""Render the UniqLab app icon for every platform.

Writes:
  uniqlab/assets/app_icon.png        1024 px, used at runtime (window/taskbar icon)
  packaging/macos/UniqLab.icns       macOS bundle icon
  packaging/windows/UniqLab.ico      Windows executable/installer icon
  packaging/linux/uniqlab.png        512 px, AppImage / desktop entry icon

Run from the repository root:  python tools/make_icon.py
The output contains no metadata (no EXIF/XMP, no text chunks).
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from uniqlab.app import paint_brand_mark, superellipse_path  # noqa: E402

# Apple's macOS icon grid: a 1024 canvas with an 824 px body centred on it.
CANVAS = 1024
BODY = QRectF(100, 100, 824, 824)


def render(size: int, full_bleed: bool = False) -> Image.Image:
    """Draw the icon at ``size`` px. ``full_bleed`` drops the macOS grid margin (Windows/Linux)."""
    scale = size / CANVAS
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    body_rect = QRectF(24, 24, 976, 976) if full_bleed else BODY
    body = QRectF(body_rect.left() * scale, body_rect.top() * scale, body_rect.width() * scale, body_rect.height() * scale)

    if size >= 64 and not full_bleed:
        # Soft drop shadow: draw the silhouette tiny and scale it up to blur it.
        small = 48
        shadow = QImage(small, small, QImage.Format_ARGB32_Premultiplied)
        shadow.fill(Qt.transparent)
        shadow_painter = QPainter(shadow)
        shadow_painter.setRenderHint(QPainter.Antialiasing, True)
        shadow_painter.setPen(Qt.NoPen)
        shadow_painter.setBrush(QColor(0, 10, 50, 105))
        k = small / CANVAS
        shadow_painter.drawPath(superellipse_path(QRectF(BODY.left() * k, (BODY.top() + 14) * k, BODY.width() * k, BODY.height() * k)))
        shadow_painter.end()
        painter.drawImage(QRectF(0, 0, size, size), shadow.scaled(size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))

    paint_brand_mark(painter, body, grid=size >= 128)

    if size >= 128:
        painter.setPen(QPen(QColor(255, 255, 255, 46), max(1.0, 3 * scale)))
        painter.setBrush(Qt.NoBrush)
        inset = 1.5 * scale
        painter.drawPath(superellipse_path(body.adjusted(inset, inset, -inset, -inset)))
    painter.end()

    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    pil = Image.open(io.BytesIO(bytes(buffer.data()))).convert("RGBA")
    pil.load()
    return pil


def _clean_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = Image.new("RGBA", image.size)
    clean.paste(image)
    clean.save(path, "PNG", optimize=True)


def main() -> None:
    QApplication([])
    _clean_png(render(1024), ROOT / "uniqlab/assets/app_icon.png")
    _clean_png(render(512, full_bleed=True), ROOT / "packaging/linux/uniqlab.png")

    mac = render(1024)
    mac.save(ROOT / "packaging/macos/UniqLab.icns", "ICNS",
             append_images=[render(size) for size in (16, 32, 64, 128, 256, 512)])

    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = {size: render(size, full_bleed=True) for size in sizes}
    frames[256].save(ROOT / "packaging/windows/UniqLab.ico", "ICO", sizes=[(s, s) for s in sizes],
                     append_images=[frames[s] for s in sizes if s != 256])
    print("Icons written: app_icon.png, UniqLab.icns, UniqLab.ico, uniqlab.png")


if __name__ == "__main__":
    main()
