"""UniqLab desktop application (PySide6): video and image uniquification with metadata presets."""

from __future__ import annotations

import math
import os
import queue
import random
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
from html import escape as html_escape
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEasingCurve, QObject, QPoint, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QComboBox,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from uniqlab import APP_NAME, AUTHOR, AUTHOR_URL, BUNDLE_ID, __version__
from uniqlab import dependencies
from uniqlab.i18n import (
    NATIVE_NAMES,
    SHORT_NAMES,
    SUPPORTED_LOCALES,
    add_listener,
    current_locale,
    decimal,
    set_locale,
    system_locale,
    tr,
)
from uniqlab.media_metadata import (
    build_plan,
    exiftool_available,
    ffmpeg_video_options,
    probe_fps,
    read_metadata,
    set_exiftool_path,
    verify_output,
    video_container,
    write_photo_metadata,
    write_video_metadata,
)
from uniqlab.metadata_presets import (
    FIELDS,
    ProfileStore,
    field_spec,
    profile_group,
    profile_name,
    profile_summary,
    validate_value,
)
from uniqlab.photo_processing import INTENSITY_KEYS, OUTPUT_EXTENSIONS, PHOTO_EXTENSIONS, PhotoError, photo_format, process_photo
from uniqlab.platform_support import (
    BUNDLED_DISPLAY_FAMILY,
    BUNDLED_TEXT_FAMILY,
    CJK_FALLBACKS,
    IS_MACOS,
    app_data_dir,
    desktop_dir,
    font_profile,
    open_folder,
    open_url,
    popen as popen_process,
    prefers_reduced_motion,
    register_fonts,
    resource_path,
    run as run_process,
)
from uniqlab.settings import AppSettings

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi"}
RANDOM_CROP_NAME = "random"
NO_CROP_NAME = "none"

# Tool locations resolved by the dependency manager (Settings → Components).
_TOOL_PATHS: dict[str, str] = {"ffmpeg": "", "exiftool": ""}


def video_filter() -> str:
    return tr("dialog.filter.video") + " (" + " ".join(f"*{ext} *{ext.upper()}" for ext in sorted(VIDEO_EXTENSIONS)) + ")"


def photo_filter() -> str:
    return tr("dialog.filter.photo") + " (" + " ".join(f"*{ext} *{ext.upper()}" for ext in sorted(PHOTO_EXTENSIONS)) + ")"


def media_noun(media: str, n: int = 2) -> str:
    """Localized "video"/"photo" noun for counts (plural-aware)."""
    return tr(f"noun.{media}", n=n)


def intensity_label(key: str) -> str:
    return tr(f"photo.intensity.{key}")


def should_set_runtime_icon() -> bool:
    # In the bundled macOS app the Dock already shows the bundle .icns, rendered by
    # macOS on the standard icon grid. Replacing it at runtime with the edge-to-edge
    # PNG makes the running app's Dock tile larger than every other app.
    return not (IS_MACOS and getattr(sys, "frozen", False))


def get_ffmpeg_path() -> str:
    return _TOOL_PATHS.get("ffmpeg") or ""


def ffmpeg_ready() -> bool:
    path = get_ffmpeg_path()
    return bool(path) and Path(path).exists()


# (id, short label, caption key). Brand captions are identical in every catalog.
ASPECT_OPTIONS = [
    ("9:16", "9:16", "aspect.tiktok"),
    ("16:9", "16:9", "aspect.youtube"),
    ("1:1", "1:1", "aspect.instagram"),
    ("4:5", "4:5", "aspect.instagram_feed"),
    ("4:3", "4:3", "aspect.classic"),
    ("3:4", "3:4", "aspect.portrait"),
    ("21:9", "21:9", "aspect.ultrawide"),
    ("2:3", "2:3", "aspect.pinterest"),
    ("3:2", "3:2", "aspect.photo"),
    ("5:4", "5:4", "aspect.large_format"),
    (RANDOM_CROP_NAME, "?:?", "aspect.random"),
]

ASPECT_LABELS = {name: (short, caption) for name, short, caption in ASPECT_OPTIONS}

ASPECT_RATIOS = {
    name: tuple(int(part) for part in short.split(":"))
    for name, short, _caption in ASPECT_OPTIONS
    if name != RANDOM_CROP_NAME
}


def format_seconds(seconds: float) -> str:
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def probe_duration(path: Path) -> float:
    result = run_process(
        [get_ffmpeg_path(), "-hide_banner", "-i", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
    if not match:
        raise ValueError(tr("error.duration"))

    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def random_output_path(output_dir: Path, prefix: str = "video", extension: str = ".mp4") -> Path:
    # Create the file exclusively so an existing file with the same name is never
    # overwritten; FFmpeg then writes into this placeholder that we own.
    while True:
        candidate = output_dir / f"{prefix}_{secrets.token_hex(3)}{extension}"
        try:
            with candidate.open("xb"):
                pass
        except FileExistsError:
            continue
        return candidate


def remove_partial_output(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def collect_videos(paths: list[Path], extensions=None) -> list[Path]:
    extensions = extensions or VIDEO_EXTENSIONS
    seen: set[Path] = set()
    videos: list[Path] = []
    for path in paths:
        if path.suffix.lower() not in extensions:
            continue
        key = path.resolve()
        if key in seen:
            continue
        seen.add(key)
        videos.append(path)
    return videos


def default_dialog_dir() -> str:
    return str(desktop_dir())


def centered_crop_filter(width_ratio: int, height_ratio: int) -> str:
    return (
        "crop="
        f"w='trunc(if(gt(iw/ih,{width_ratio}/{height_ratio}),ih*{width_ratio}/{height_ratio},iw)/2)*2':"
        f"h='trunc(if(gt(iw/ih,{width_ratio}/{height_ratio}),ih,iw*{height_ratio}/{width_ratio})/2)*2':"
        "x='(iw-ow)/2':"
        "y='(ih-oh)/2'"
    )


def random_crop_filter() -> str:
    width_scale = random.uniform(0.90, 0.98)
    height_scale = random.uniform(0.90, 0.98)
    x_position = random.uniform(0.0, 1.0)
    y_position = random.uniform(0.0, 1.0)
    return (
        "crop="
        f"w='trunc(iw*{width_scale:.4f}/2)*2':"
        f"h='trunc(ih*{height_scale:.4f}/2)*2':"
        f"x='trunc((iw-ow)*{x_position:.4f}/2)*2':"
        f"y='trunc((ih-oh)*{y_position:.4f}/2)*2'"
    )


def crop_filter_for_mode(aspect_name: str) -> str | None:
    if aspect_name == RANDOM_CROP_NAME:
        return random_crop_filter()

    if aspect_name == NO_CROP_NAME:
        return None

    width_ratio, height_ratio = ASPECT_RATIOS[aspect_name]
    return centered_crop_filter(width_ratio, height_ratio)


def build_filter_chain(aspect_name: str) -> str:
    filters = []
    crop_filter = crop_filter_for_mode(aspect_name)
    if crop_filter:
        filters.append(crop_filter)

    noise_filter = "noise=alls=1:allf=t+u"
    filters.append(noise_filter)

    if random.choice([True, False]):
        value = random.choice([-1, 1]) * random.uniform(0.01, 0.02)
        color_filter = f"eq=brightness={value:.4f}"
    else:
        value = 1 + random.choice([-1, 1]) * random.uniform(0.01, 0.02)
        color_filter = f"eq=contrast={value:.4f}"

    filters.append(color_filter)
    return ",".join(filters)


class ProcessingStopped(Exception):
    pass


BRAND_BLUE = "#0038CC"
BRAND_BLUE_HOVER = "#0F48EA"
BRAND_BLUE_PRESSED = "#002DA6"
BRAND_NAVY = "#001A6E"
BRAND_LIME = "#AEFC00"
BRAND_ERROR = "#FF6B6B"

# The cobalt header and processing panel are the brand and stay the same in both
# appearances; the content sheet follows Light / Dark.
THEMES: dict[str, dict[str, str]] = {
    "light": {
        "sheet": "#FFFFFF",
        "card": "#F1F3F8",
        "field": "#FFFFFF",
        "field_border": "#DFE4F0",
        "text": "#060E30",
        "muted": "#5C6690",
        "faint": "#A6AECB",
        "accent_text": BRAND_BLUE,
        "outline_hover": "#E4E9F6",
        "disabled_bg": "#E3E7F0",
        "disabled_text": "#A0A8C4",
        "log_text": "#1A2453",
        "selection": "#C3D3FF",
        "menu_bg": "#FFFFFF",
        "menu_hover": "#EEF2FC",
        "menu_border": "#DCE2F0",
    },
    "dark": {
        "sheet": "#050A22",
        "card": "#0C1435",
        "field": "#121C46",
        "field_border": "#1E2A5C",
        "text": "#F2F4FF",
        "muted": "#8D98C5",
        "faint": "#4B5789",
        "accent_text": "#7395FF",
        "outline_hover": "#16214D",
        "disabled_bg": "#141D44",
        "disabled_text": "#4B5789",
        "log_text": "#C8D1F4",
        "selection": "#2A46B5",
        "menu_bg": "#0F1840",
        "menu_hover": "#18245A",
        "menu_border": "#26336E",
    },
}

FOLDER_ROLE = Qt.UserRole + 1
SIZE_ROLE = Qt.UserRole + 2

_SYSTEM_FAMILY: str | None = None


def system_family() -> str:
    global _SYSTEM_FAMILY
    if _SYSTEM_FAMILY is None:
        _SYSTEM_FAMILY = QFontDatabase.systemFont(QFontDatabase.GeneralFont).family()
    return _SYSTEM_FAMILY


def _families(primary: str) -> list[str]:
    # Brand fonts have no Han glyphs; list CJK system fonts so Simplified Chinese never
    # falls back to boxes or to a random face.
    return [primary, *CJK_FALLBACKS]


def display_font(pixel_size: int, width: float = 150) -> QFont:
    if font_profile() == "apple":
        # SF Pro Expanded Black (system font, not redistributed): the wide, heavy grotesque of the brand.
        font = QFont(system_family())
        font.setFamilies(_families(system_family()))
        font.setPixelSize(pixel_size)
        font.setWeight(QFont.Black)
        if hasattr(font, "setVariableAxis"):
            font.setVariableAxis(QFont.Tag("wdth"), width)
        return font
    # Unbounded (OFL) is natively wide; it is drawn slightly smaller to match SF Expanded metrics.
    font = QFont(BUNDLED_DISPLAY_FAMILY)
    font.setFamilies(_families(BUNDLED_DISPLAY_FAMILY))
    font.setPixelSize(max(1, round(pixel_size * (0.92 if width >= 140 else 0.86))))
    font.setWeight(QFont.Black)
    return font


def ui_font(pixel_size: int, weight: QFont.Weight = QFont.Medium) -> QFont:
    family = system_family() if font_profile() == "apple" else BUNDLED_TEXT_FAMILY
    font = QFont(family)
    font.setFamilies(_families(family))
    font.setPixelSize(pixel_size)
    font.setWeight(weight)
    return font


def mono_font(pixel_size: int = 12) -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    font.setPixelSize(pixel_size)
    return font


def format_size(size: int) -> str:
    if size >= 1024**3:
        value, unit = size / 1024**3, "unit.gb"
    elif size >= 1024**2:
        value, unit = size / 1024**2, "unit.mb"
    else:
        value, unit = max(size, 0) / 1024, "unit.kb"
    return f"{decimal(value)} {tr(unit)}"



def spring_progress(t: float, damping: float = 0.75, frequency: float = 7.5) -> float:
    """Damped spring from 0 to 1 over t in [0, 1]: eased start, ~3% overshoot, soft settle."""
    if t >= 1.0:
        return 1.0
    damped = frequency * math.sqrt(1 - damping * damping)
    envelope = math.exp(-damping * frequency * t)
    return 1 - envelope * (math.cos(damped * t) + damping * frequency / damped * math.sin(damped * t))


def superellipse_path(rect: QRectF, exponent: float = 5.0, steps: int = 240) -> QPainterPath:
    # Continuous-corner squircle, the silhouette of macOS app icons.
    cx, cy = rect.center().x(), rect.center().y()
    a, b = rect.width() / 2, rect.height() / 2
    path = QPainterPath()
    for step in range(steps):
        t = 2 * math.pi * step / steps
        c, s = math.cos(t), math.sin(t)
        point = QPointF(
            cx + a * math.copysign(abs(c) ** (2 / exponent), c),
            cy + b * math.copysign(abs(s) ** (2 / exponent), s),
        )
        if step == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    path.closeSubpath()
    return path


def brand_glyph_paths(rect: QRectF) -> tuple[QPainterPath, QPainterPath]:
    """The "Ui" glyph of the logo: a U whose right arm is the stem of an i."""
    s = rect.width()
    x0, y0 = rect.left(), rect.top()
    left_x = x0 + 0.295 * s
    right_x = x0 + 0.705 * s
    radius = (right_x - left_x) / 2
    arc_cy = y0 + 0.555 * s

    centerline = QPainterPath(QPointF(left_x, y0 + 0.24 * s))
    centerline.lineTo(left_x, arc_cy)
    centerline.arcTo(QRectF(left_x, arc_cy - radius, radius * 2, radius * 2), 180, 180)
    centerline.lineTo(right_x, y0 + 0.50 * s)

    stroker = QPainterPathStroker()
    stroker.setWidth(0.17 * s)
    stroker.setCapStyle(Qt.RoundCap)
    stroker.setJoinStyle(Qt.RoundJoin)
    u_shape = stroker.createStroke(centerline).simplified()

    dot = QPainterPath()
    dot.addEllipse(QPointF(right_x, y0 + 0.27 * s), 0.095 * s, 0.095 * s)
    return u_shape, dot


def paint_brand_mark(painter: QPainter, rect: QRectF, grid: bool = True) -> None:
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    shape = superellipse_path(rect)

    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, QColor("#1450FF"))
    gradient.setColorAt(1.0, QColor("#002FB0"))
    painter.setPen(Qt.NoPen)
    painter.setBrush(gradient)
    painter.drawPath(shape)

    if grid:
        painter.setClipPath(shape)
        painter.setPen(QPen(QColor(255, 255, 255, 26), max(1.0, rect.width() / 300)))
        step = rect.width() / 7
        for index in range(1, 7):
            offset = index * step
            painter.drawLine(QPointF(rect.left() + offset, rect.top()), QPointF(rect.left() + offset, rect.bottom()))
            painter.drawLine(QPointF(rect.left(), rect.top() + offset), QPointF(rect.right(), rect.top() + offset))
        painter.setClipping(False)

    u_shape, dot = brand_glyph_paths(rect)
    depth = QPointF(rect.width() * 0.028, rect.width() * 0.036)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 16, 80, 150))
    painter.drawPath(u_shape.translated(depth))
    painter.drawPath(dot.translated(depth))
    painter.setBrush(QColor("#FFFFFF"))
    painter.drawPath(u_shape)
    painter.setBrush(QColor(BRAND_LIME))
    painter.drawPath(dot)
    painter.restore()


def paint_grid(painter: QPainter, rect: QRectF, step: int, alpha: int = 20) -> None:
    painter.setPen(QPen(QColor(255, 255, 255, alpha), 1))
    x = rect.left() + step
    while x < rect.right():
        painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        x += step
    y = rect.top() + step
    while y < rect.bottom():
        painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        y += step


class CobaltPanel(QFrame):
    """Rounded cobalt surface with the brand grid, used for the processing panel."""

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 28, 28)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(BRAND_BLUE))
        painter.drawPath(path)
        painter.setClipPath(path)
        paint_grid(painter, QRectF(self.rect()), 38, 18)


class CreditLink(QAbstractButton):
    """Subtle author credit in the header; opens the author's GitHub profile."""

    TEXT = f"by {AUTHOR}"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._font = ui_font(10, QFont.DemiBold)
        self._font.setLetterSpacing(QFont.PercentageSpacing, 104)
        metrics = QFontMetricsF(self._font)
        self.setFixedSize(int(math.ceil(metrics.horizontalAdvance(self.TEXT))) + 6, int(math.ceil(metrics.height())) + 4)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAccessibleName(self.TEXT)
        self.clicked.connect(lambda: open_url(AUTHOR_URL))
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self.setToolTip(tr("header.credit_tooltip", url=AUTHOR_URL))
        self.setAccessibleDescription(tr("header.credit_tooltip", url=AUTHOR_URL))

    def baseline(self) -> float:
        return 2 + QFontMetricsF(self._font).ascent()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        hot = self.underMouse() or self.hasFocus()
        # The header stays cobalt in both themes, so one palette serves light and dark.
        color = QColor(BRAND_LIME) if hot else QColor(255, 255, 255, 128)
        painter.setFont(self._font)
        painter.setPen(color)
        metrics = QFontMetricsF(self._font)
        painter.drawText(QPointF(3, self.baseline()), self.TEXT)
        if hot:
            y = self.baseline() + 2
            painter.setPen(QPen(color, 1))
            painter.drawLine(QPointF(3, y), QPointF(3 + metrics.horizontalAdvance(self.TEXT), y))


class BrandWordmark(QWidget):
    MARK = 42
    NAME = "UNIQLAB"
    CAPTION = "VIDEO & IMAGE PROCESSING"
    CREDIT_GAP = 12

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._font = display_font(18)
        self._caption_font = ui_font(10, QFont.Bold)
        self._caption_font.setLetterSpacing(QFont.PercentageSpacing, 114)
        self.credit = CreditLink(self)
        metrics = QFontMetricsF(self._font)
        self._hash_width = metrics.horizontalAdvance("#")
        self._caption_width = QFontMetricsF(self._caption_font).horizontalAdvance(self.CAPTION)
        text_width = max(
            metrics.horizontalAdvance("#" + self.NAME), self._caption_width + self.CREDIT_GAP + self.credit.width()
        )
        self.setFixedSize(int(math.ceil(self.MARK + 14 + text_width)) + 2, 46)
        self.setAccessibleName(f"{APP_NAME} — {self.CAPTION}")
        self._place_credit()

    def _caption_baseline(self) -> float:
        top = (self.height() - self.MARK) / 2
        return top + self.MARK - 3 - QFontMetricsF(self._caption_font).descent()

    def _place_credit(self) -> None:
        x = self.MARK + 14 + 1 + self._caption_width + self.CREDIT_GAP - 3
        y = self._caption_baseline() - self.credit.baseline()
        self.credit.move(int(round(x)), int(round(y)))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        top = (self.height() - self.MARK) / 2
        paint_brand_mark(painter, QRectF(0, top, self.MARK, self.MARK), grid=False)
        x = self.MARK + 14
        metrics = QFontMetricsF(self._font)
        baseline = top + 3 + metrics.capHeight()
        painter.setFont(self._font)
        painter.setPen(QColor(BRAND_LIME))
        painter.drawText(QPointF(x, baseline), "#")
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QPointF(x + self._hash_width, baseline), self.NAME)
        painter.setFont(self._caption_font)
        painter.setPen(QColor(255, 255, 255, 170))
        painter.drawText(QPointF(x + 1, self._caption_baseline()), self.CAPTION)


class ThemeSwitch(QFrame):
    """Segmented control on a translucent track; one white pill slides between the options."""

    DURATION = 380
    THEME_OPTIONS = (("light", "theme.light"), ("dark", "theme.dark"), ("system", "theme.auto"))

    def __init__(self, parent: QWidget | None = None, options: tuple = THEME_OPTIONS, padding: int = 16) -> None:
        super().__init__(parent)
        self.setFixedHeight(40)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)
        self.buttons: dict[str, PillButton] = {}
        self.option_keys = dict(options)  # mode -> i18n key
        for mode, key in options:
            button = PillButton(tr(key), "segment")
            button.setCheckable(True)
            self.buttons[mode] = button
            layout.addWidget(button)
        self.mode: str | None = None
        self._pill = QRectF()
        self._from = QRectF()
        self._to = QRectF()
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(self.DURATION)
        self._animation.valueChanged.connect(self._step)

    def set_mode(self, mode: str, animate: bool = True) -> None:
        self.mode = mode
        for key, button in self.buttons.items():
            button.setChecked(key == mode)
        target = self._target(mode)
        if not animate or self._pill.isNull() or target.isNull() or prefers_reduced_motion():
            self._animation.stop()
            self._pill = target
            self._sync()
            return
        # Start from wherever the pill is right now, so rapid clicks never jump.
        self._from = QRectF(self._pill)
        self._to = target
        self._animation.stop()
        self._animation.start()

    def retranslate_ui(self) -> None:
        for mode, button in self.buttons.items():
            button.setText(tr(self.option_keys[mode]))
            button.updateGeometry()
        if self.mode:
            QTimer.singleShot(0, lambda: self.set_mode(self.mode, animate=False))

    def _target(self, mode: str | None) -> QRectF:
        button = self.buttons.get(mode or "")
        return QRectF(button.geometry()) if button and button.width() > 0 else QRectF()

    def _step(self, value: float) -> None:
        k = spring_progress(float(value))
        left = self._from.left() + (self._to.left() - self._from.left()) * k
        right = self._from.right() + (self._to.right() - self._from.right()) * k
        # The spring may overshoot a little; never let the pill leave the track.
        left, right = max(1.0, left), min(self.width() - 1.0, right)
        self._pill = QRectF(left, self._to.top(), max(self._to.height(), right - left), self._to.height())
        self._sync()

    def _sync(self) -> None:
        for button in self.buttons.values():
            geometry = QRectF(button.geometry())
            overlap = min(self._pill.right(), geometry.right()) - max(self._pill.left(), geometry.left())
            button.highlight = max(0.0, min(1.0, overlap / geometry.width())) if geometry.width() else 0.0
            button.update()
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._animation.state() != QVariantAnimation.Running and self.mode:
            QTimer.singleShot(0, lambda: self.set_mode(self.mode, animate=False))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.mode:
            QTimer.singleShot(0, lambda: self.set_mode(self.mode, animate=False))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 34))
        rect = QRectF(self.rect())
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        if not self._pill.isNull():
            pill = self._pill.adjusted(1, 1, -1, -1)
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)


class ThemeButton(QAbstractButton):
    """Small round theme button in the header: cycles Light → Dark → Auto."""

    LABELS = {"light": "theme.light", "dark": "theme.dark", "system": "theme.auto_system"}

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = "system"
        self.setFixedSize(40, 40)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self.setAccessibleName(tr("theme.accessible"))
        self.setToolTip(tr("theme.tooltip", theme=tr(self.LABELS.get(self.mode, "theme.auto_system"))))

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.retranslate_ui()
        self.update()

    def next_mode(self) -> str:
        order = ["light", "dark", "system"]
        return order[(order.index(self.mode) + 1) % 3] if self.mode in order else "light"

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 70 if self.isDown() else 52 if self.underMouse() else 34))
        painter.drawEllipse(rect)
        center = rect.center()
        white = QColor("#FFFFFF")
        if self.mode == "light":
            painter.setBrush(white)
            painter.drawEllipse(center, 4.6, 4.6)
            pen = QPen(white, 1.8)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            for index in range(8):
                angle = index * math.pi / 4
                painter.drawLine(
                    QPointF(center.x() + math.cos(angle) * 7.4, center.y() + math.sin(angle) * 7.4),
                    QPointF(center.x() + math.cos(angle) * 10, center.y() + math.sin(angle) * 10),
                )
        elif self.mode == "dark":
            moon = QPainterPath()
            moon.addEllipse(center, 8.5, 8.5)
            bite = QPainterPath()
            bite.addEllipse(QPointF(center.x() + 4.6, center.y() - 3.6), 7.2, 7.2)
            painter.setBrush(white)
            painter.drawPath(moon.subtracted(bite))
        else:
            painter.setPen(QPen(white, 1.8))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, 8.5, 8.5)
            half = QPainterPath()
            half.moveTo(center)
            half.arcTo(QRectF(center.x() - 8.5, center.y() - 8.5, 17, 17), 90, 180)
            half.closeSubpath()
            painter.setPen(Qt.NoPen)
            painter.setBrush(white)
            painter.drawPath(half)


class HeaderDivider(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(1, 26)

    def paintEvent(self, event) -> None:
        QPainter(self).fillRect(self.rect(), QColor(255, 255, 255, 60))


class StatusPill(QAbstractButton):
    """Outlined pill with a state dot, drawn on cobalt. Clickable when ``interactive``."""

    DOT_COLORS = {"ok": BRAND_LIME, "error": BRAND_ERROR, "check": "#FFFFFF", "warn": "#FFD25A"}

    def __init__(self, height: int = 40, font_size: int = 13, interactive: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = "check"
        self.label = tr("status.checking")
        self.interactive = interactive
        self._font = ui_font(font_size, QFont.DemiBold)
        self.setFixedHeight(height)
        if interactive:
            self.setCursor(Qt.PointingHandCursor)
            self.setAttribute(Qt.WA_Hover, True)
            self.setFocusPolicy(Qt.TabFocus)
        else:
            self.setFocusPolicy(Qt.NoFocus)
            self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_state(self, state: str, text: str) -> None:
        self.state = state
        self.label = text
        self.setAccessibleName(text)
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        padding = self.height() * 1.45
        return QSize(int(QFontMetricsF(self._font).horizontalAdvance(self.label) + padding), self.height())

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75)
        hot = self.interactive and (self.underMouse() or self.hasFocus())
        painter.setPen(QPen(QColor(255, 255, 255, 230 if hot else 170), 1.5))
        painter.setBrush(QColor(255, 255, 255, 46 if self.isDown() else 26) if hot else Qt.NoBrush)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        dot_x = rect.height() * 0.58
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self.DOT_COLORS.get(self.state, "#FFFFFF")))
        painter.drawEllipse(QPointF(dot_x, rect.center().y()), 4.5, 4.5)
        painter.setFont(self._font)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(rect.adjusted(dot_x + 13, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, self.label)


class DisplayLabel(QWidget):
    """Tight-leading display type in the brand face."""

    def __init__(
        self,
        text: str,
        pixel_size: int,
        color_key: str = "text",
        align: Qt.AlignmentFlag = Qt.AlignLeft,
        width_axis: float = 150,
        fixed_color: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._text = text
        self._font = display_font(pixel_size, width_axis)
        self._leading = pixel_size * 1.02
        self.color_key = color_key
        self.fixed_color = fixed_color
        self.align = align
        self._color = QColor(fixed_color or THEMES["light"][color_key])
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text
        self.updateGeometry()
        self.update()

    def set_theme(self, tokens: dict[str, str]) -> None:
        if not self.fixed_color:
            self._color = QColor(tokens[self.color_key])
            self.update()

    def sizeHint(self) -> QSize:
        metrics = QFontMetricsF(self._font)
        lines = self._text.split("\n")
        width = max(metrics.horizontalAdvance(line) for line in lines)
        height = self._leading * (len(lines) - 1) + metrics.ascent() + metrics.descent()
        return QSize(int(math.ceil(width)) + 2, int(math.ceil(height)))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setFont(self._font)
        painter.setPen(self._color)
        metrics = QFontMetricsF(self._font)
        for index, line in enumerate(self._text.split("\n")):
            line_width = metrics.horizontalAdvance(line)
            if self.align & Qt.AlignHCenter:
                x = (self.width() - line_width) / 2
            elif self.align & Qt.AlignRight:
                x = self.width() - line_width
            else:
                x = 0
            painter.drawText(QPointF(x, metrics.ascent() + index * self._leading), line)


class MetricLabel(QWidget):
    """The big number of the processing panel; shrinks to fit narrow widths."""

    def __init__(self, pixel_size: int = 60, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = ""
        self._max_size = pixel_size
        self._fonts: dict[int, QFont] = {}
        cap = QFontMetricsF(display_font(pixel_size)).capHeight()
        self.setFixedHeight(int(math.ceil(cap)) + 4)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumWidth(40)

    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.update()

    def _font(self, size: int) -> QFont:
        if size not in self._fonts:
            self._fonts[size] = display_font(size)
        return self._fonts[size]

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        size = self._max_size
        width = QFontMetricsF(self._font(size)).horizontalAdvance(self._text)
        if width > self.width():
            size = max(18, int(size * self.width() / width))
        painter.setFont(self._font(size))
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QPointF(0, self.height() - 2), self._text)


class ElidedLabel(QWidget):
    """Single-line text that elides instead of stretching the layout."""

    def __init__(
        self,
        font: QFont,
        color_key: str = "muted",
        fixed_color: QColor | None = None,
        elide: Qt.TextElideMode = Qt.ElideRight,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._text = ""
        self._font = font
        self.color_key = color_key
        self.fixed_color = fixed_color
        self._color = QColor(fixed_color) if fixed_color else QColor(THEMES["light"][color_key])
        self._elide = elide
        self.setFixedHeight(int(math.ceil(QFontMetricsF(font).height())) + 2)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumWidth(20)

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.update()

    def set_theme(self, tokens: dict[str, str]) -> None:
        if not self.fixed_color:
            self._color = QColor(tokens[self.color_key])
            self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setFont(self._font)
        painter.setPen(self._color)
        metrics = QFontMetricsF(self._font)
        if self._elide == Qt.ElideNone:
            text = self._text if metrics.horizontalAdvance(self._text) <= self.width() else ""
        else:
            text = metrics.elidedText(self._text, self._elide, self.width())
        painter.drawText(QRectF(self.rect()), Qt.AlignVCenter | Qt.AlignLeft, text)


class PillButton(QPushButton):
    """Pill button: "primary", "outline", "chip" (toggle), "segment" and "ghost" (both on cobalt)."""

    def __init__(
        self,
        text: str = "",
        variant: str = "primary",
        chevron: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self.variant = variant
        self.chevron = chevron
        self.highlight = 0.0
        self.tokens = THEMES["light"]
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        if variant == "chip":
            self.setFont(display_font(12, 130))
            self.setCheckable(True)
        else:
            self.setFont(ui_font(13, QFont.DemiBold))
        self.setFixedHeight({"segment": 32, "ghost": 32, "chip": 36}.get(variant, 40))

    def set_theme(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens
        self.update()

    def sizeHint(self) -> QSize:
        padding = {"segment": 16, "ghost": 14, "chip": 15}.get(self.variant, 20)
        width = QFontMetricsF(self.font()).horizontalAdvance(self.text()) + padding * 2
        if self.chevron:
            width += 18
        return QSize(int(math.ceil(width)), self.height())

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        tokens = self.tokens
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        hovered = self.underMouse()
        pressed = self.isDown()
        enabled = self.isEnabled()
        pen: QPen | Qt.PenStyle = Qt.NoPen
        brush: QColor | Qt.BrushStyle = Qt.NoBrush

        if self.variant == "primary" or (self.variant == "chip" and self.isChecked()):
            if enabled:
                brush = QColor(BRAND_BLUE_PRESSED if pressed else BRAND_BLUE_HOVER if hovered else BRAND_BLUE)
                text_color = QColor("#FFFFFF")
            else:
                brush = QColor(tokens["disabled_bg"])
                text_color = QColor(tokens["disabled_text"])
        elif self.variant == "chip":
            text_color = QColor(tokens["text"] if enabled else tokens["disabled_text"])
            brush = QColor(tokens["outline_hover"] if enabled and (hovered or pressed) else tokens["field"])
            pen = QPen(QColor(tokens["field_border"]), 1)
        elif self.variant == "outline":
            text_color = QColor(tokens["text"] if enabled else tokens["disabled_text"])
            pen = QPen(text_color, 1.5)
            if enabled and (hovered or pressed):
                brush = QColor(tokens["outline_hover"])
        elif self.variant == "segment":
            # The white pill itself is drawn and animated by ThemeSwitch.
            idle = QColor(255, 255, 255, 255 if hovered else 205)
            navy = QColor(BRAND_NAVY)
            k = self.highlight
            text_color = QColor(
                round(idle.red() + (navy.red() - idle.red()) * k),
                round(idle.green() + (navy.green() - idle.green()) * k),
                round(idle.blue() + (navy.blue() - idle.blue()) * k),
                round(idle.alpha() + (navy.alpha() - idle.alpha()) * k),
            )
            if k < 0.5 and (hovered or pressed):
                brush = QColor(255, 255, 255, 40 if pressed else 26)
        else:
            pen = QPen(QColor(255, 255, 255, 235 if hovered else 125), 1.2)
            text_color = QColor("#FFFFFF")
            if pressed or (self.variant == "ghost" and hovered):
                brush = QColor(255, 255, 255, 36)

        painter.setPen(pen)
        painter.setBrush(brush)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)

        text_rect = rect.adjusted(0, 0, -16, 0) if self.chevron else rect
        painter.setPen(text_color)
        painter.setFont(self.font())
        painter.drawText(text_rect, Qt.AlignCenter, self.text())

        if self.chevron:
            text_width = QFontMetricsF(self.font()).horizontalAdvance(self.text())
            x = text_rect.center().x() + text_width / 2 + 12
            y = rect.center().y()
            chevron = QPainterPath(QPointF(x - 4, y - 1.8))
            chevron.lineTo(x, y + 2.2)
            chevron.lineTo(x + 4, y - 1.8)
            chevron_pen = QPen(text_color, 1.7)
            chevron_pen.setCapStyle(Qt.RoundCap)
            chevron_pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(chevron_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(chevron)


class ActionButton(QPushButton):
    """The single primary action of the processing panel: start, or stop while running."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(tr("action.start"), parent)
        self.mode = "start"
        self.setFont(ui_font(16, QFont.Bold))
        self.setFixedHeight(58)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_mode(self, mode: str, text: str) -> None:
        self.mode = mode
        self.setText(text)
        self.update()

    def minimumSizeHint(self) -> QSize:
        return QSize(180, 58)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = rect.height() / 2
        enabled = self.isEnabled()
        hovered = self.underMouse()
        pressed = self.isDown()
        icon_center = QPointF(rect.left() + radius, rect.center().y())

        if self.mode == "stop":
            pen = QPen(QColor(255, 255, 255, 230 if enabled else 110), 1.6)
            painter.setPen(pen)
            painter.setBrush(QColor(255, 255, 255, 40 if enabled and (hovered or pressed) else 0))
            painter.drawRoundedRect(rect, radius, radius)
            text_color = QColor(255, 255, 255, 255 if enabled else 140)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 46))
            painter.drawEllipse(icon_center, 16, 16)
            painter.setBrush(text_color)
            painter.drawRoundedRect(QRectF(icon_center.x() - 5.5, icon_center.y() - 5.5, 11, 11), 2.5, 2.5)
        else:
            if enabled:
                fill = QColor("#DCE5FF" if pressed else "#EEF3FF" if hovered else "#FFFFFF")
                text_color = QColor(BRAND_BLUE)
                core, glyph = QColor(BRAND_LIME), QColor(BRAND_NAVY)
            else:
                fill = QColor(255, 255, 255, 46)
                text_color = QColor(255, 255, 255, 150)
                core, glyph = QColor(255, 255, 255, 60), QColor(255, 255, 255, 150)
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, radius, radius)
            painter.setBrush(core)
            painter.drawEllipse(icon_center, 18, 18)
            triangle = QPainterPath(QPointF(icon_center.x() - 4, icon_center.y() - 7))
            triangle.lineTo(icon_center.x() - 4, icon_center.y() + 7)
            triangle.lineTo(icon_center.x() + 7.5, icon_center.y())
            triangle.closeSubpath()
            painter.setBrush(glyph)
            painter.drawPath(triangle)

        painter.setPen(text_color)
        painter.setFont(self.font())
        painter.drawText(rect.adjusted(radius, 0, -radius * 0.6, 0), Qt.AlignCenter, self.text())


class StatTile(QWidget):
    def __init__(self, label_key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label_key = label_key
        self.value = "—"
        self._label_font = ui_font(10, QFont.Bold)
        self._label_font.setLetterSpacing(QFont.PercentageSpacing, 112)
        self._value_font = display_font(17)
        self.setFixedHeight(58)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumWidth(70)

    def set_value(self, value: str) -> None:
        if value != self.value:
            self.value = value
            self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 30))
        painter.drawRoundedRect(rect, 16, 16)
        painter.setFont(self._label_font)
        painter.setPen(QColor(255, 255, 255, 175))
        label = QFontMetricsF(self._label_font).elidedText(tr(self.label_key).upper(), Qt.ElideRight, rect.width() - 24)
        painter.drawText(QPointF(14, 21), label)
        font = self._value_font
        room = rect.width() - 26
        width = QFontMetricsF(font).horizontalAdvance(self.value)
        if width > room:
            font = display_font(max(11, int(17 * room / width)))
        painter.setFont(font)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QPointF(14, 45), self.value)


class RecapView(QWidget):
    """What the next run will use, right above the start button; drops rows when short on room."""

    ROW = 34

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: list[tuple[str, str]] = []
        self._label_font = ui_font(10, QFont.Bold)
        self._label_font.setLetterSpacing(QFont.PercentageSpacing, 112)
        self._value_font = ui_font(13, QFont.DemiBold)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

    def set_rows(self, rows: list[tuple[str, str]]) -> None:
        if rows != self.rows:
            self.rows = rows
            self.updateGeometry()
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(200, self.ROW * len(self.rows))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        label_metrics = QFontMetricsF(self._label_font)
        value_metrics = QFontMetricsF(self._value_font)
        visible = min(len(self.rows), self.height() // self.ROW)
        top = self.height() - visible * self.ROW
        for index, (label, value) in enumerate(self.rows[len(self.rows) - visible:]):
            y = top + index * self.ROW
            painter.setPen(QPen(QColor(255, 255, 255, 42), 1))
            painter.drawLine(QPointF(0, y + 0.5), QPointF(self.width(), y + 0.5))
            center_y = y + self.ROW / 2 + 1
            painter.setFont(self._label_font)
            painter.setPen(QColor(255, 255, 255, 165))
            painter.drawText(QPointF(0, center_y + label_metrics.capHeight() / 2), label)
            painter.setFont(self._value_font)
            painter.setPen(QColor("#FFFFFF"))
            room = self.width() - min(label_metrics.horizontalAdvance(label), self.width() * 0.45) - 16
            text = value_metrics.elidedText(value, Qt.ElideMiddle, room)
            painter.drawText(
                QPointF(self.width() - value_metrics.horizontalAdvance(text), center_y + value_metrics.capHeight() / 2),
                text,
            )


class NoticeBox(QWidget):
    """Problem summary on the processing panel, visible even with the log collapsed."""

    LINES = 3

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.message = ""
        self._font = ui_font(12, QFont.Medium)
        line_height = QFontMetricsF(self._font).lineSpacing()
        self.setFixedHeight(int(math.ceil(line_height * self.LINES)) + 22)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.hide()

    def set_message(self, text: str) -> None:
        self.message = " ".join(text.split())
        self.setToolTip(text if text else "")
        self.setVisible(bool(self.message))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 12, 60, 70))
        painter.drawRoundedRect(QRectF(self.rect()), 14, 14)
        painter.setBrush(QColor(BRAND_ERROR))
        painter.drawEllipse(QPointF(17, 19), 4.5, 4.5)

        area = QRectF(32, 10, self.width() - 46, self.height() - 20)
        metrics = QFontMetricsF(self._font)
        flags = Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop
        text = self.message
        # Shorten until it fits the fixed number of lines; the full text is in the tooltip and log.
        while text and metrics.boundingRect(area, flags, text).height() > area.height():
            text = text[: max(0, int(len(text) * 0.9) - 1)].rstrip() + "…"
            if len(text) <= 2:
                break
        painter.setFont(self._font)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(area, flags, text)


class ProgressLine(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value = 0
        self.setFixedHeight(16)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def value(self) -> int:
        return self._value

    def setValue(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        if value != self._value:
            self._value = value
            self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        knob = 7.0
        track = QRectF(knob, self.height() / 2 - 4, self.width() - knob * 2, 8)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 56))
        painter.drawRoundedRect(track, 4, 4)
        if self._value <= 0:
            return
        fill = QRectF(track.left(), track.top(), track.width() * self._value / 100, track.height())
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawRoundedRect(fill, 4, 4)
        painter.setPen(QPen(QColor(BRAND_BLUE), 2.5))
        painter.setBrush(QColor(BRAND_LIME))
        painter.drawEllipse(QPointF(fill.right(), track.center().y()), knob, knob)


class FileListDelegate(QStyledItemDelegate):
    ROW = 34

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = THEMES["light"]
        self._number_font = display_font(10)
        self._name_font = ui_font(13, QFont.DemiBold)
        self._meta_font = ui_font(12, QFont.Medium)

    def sizeHint(self, option, index) -> QSize:
        return QSize(0, self.ROW)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        tokens = self.tokens
        rect = QRectF(option.rect).adjusted(2, 1, -2, -1)
        if option.state & QStyle.State_MouseOver:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(tokens["menu_hover"]))
            painter.drawRoundedRect(rect, 10, 10)

        center_y = rect.center().y()
        number_metrics = QFontMetricsF(self._number_font)
        name_metrics = QFontMetricsF(self._name_font)
        meta_metrics = QFontMetricsF(self._meta_font)
        x = rect.left() + 12
        painter.setFont(self._number_font)
        painter.setPen(QColor(tokens["accent_text"]))
        painter.drawText(QPointF(x, center_y + number_metrics.capHeight() / 2), f"{index.row() + 1:02d}")
        x += 34

        size_text = str(index.data(SIZE_ROLE) or "")
        right = rect.right() - 12
        size_width = meta_metrics.horizontalAdvance(size_text)
        available = max(0.0, right - size_width - 16 - x)
        name_width = available * 0.6
        folder_width = available - name_width - 14

        painter.setFont(self._name_font)
        painter.setPen(QColor(tokens["text"]))
        name = name_metrics.elidedText(str(index.data(Qt.DisplayRole)), Qt.ElideMiddle, name_width)
        painter.drawText(QPointF(x, center_y + name_metrics.capHeight() / 2), name)

        painter.setFont(self._meta_font)
        painter.setPen(QColor(tokens["muted"]))
        folder = meta_metrics.elidedText(str(index.data(FOLDER_ROLE) or ""), Qt.ElideMiddle, folder_width)
        painter.drawText(QPointF(x + name_width + 14, center_y + meta_metrics.capHeight() / 2), folder)
        painter.drawText(QPointF(right - size_width, center_y + meta_metrics.capHeight() / 2), size_text)
        painter.restore()


class FileList(QListWidget):
    """Compact, scrollable list of the selected videos with an empty state."""

    browseRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("FileList")
        self.tokens = THEMES["light"]
        self.delegate = FileListDelegate(self)
        self.setItemDelegate(self.delegate)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMinimumHeight(FileListDelegate.ROW * 2 + 12)
        self.setProperty("empty", True)
        self.viewport().setCursor(Qt.PointingHandCursor)
        self._title_font = ui_font(14, QFont.DemiBold)
        self._hint_font = ui_font(12, QFont.Medium)
        self.empty_title_key = "files.empty.video"

    def set_theme(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens
        self.delegate.tokens = tokens
        self.viewport().update()

    def set_videos(self, videos: list[tuple[Path, int]]) -> None:
        self.clear()
        for path, size in videos:
            item = QListWidgetItem(path.name)
            item.setData(FOLDER_ROLE, compact_path(path.parent, 48))
            item.setData(SIZE_ROLE, format_size(size))
            item.setToolTip(str(path))
            self.addItem(item)
        self.setProperty("empty", not videos)
        self.viewport().setCursor(Qt.ArrowCursor if videos else Qt.PointingHandCursor)
        self.style().unpolish(self)
        self.style().polish(self)
        self.viewport().update()

    def mouseReleaseEvent(self, event) -> None:
        if not self.count() and self.isEnabled() and event.button() == Qt.LeftButton:
            self.browseRequested.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.count():
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.Antialiasing, True)
        tokens = self.tokens
        rect = QRectF(self.viewport().rect())
        center = rect.center()

        frame = QRectF(center.x() - 17, center.y() - 40, 34, 26)
        painter.setPen(QPen(QColor(tokens["accent_text"]), 1.8))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(frame, 7, 7)
        triangle = QPainterPath(QPointF(frame.center().x() - 3.5, frame.center().y() - 5.5))
        triangle.lineTo(frame.center().x() - 3.5, frame.center().y() + 5.5)
        triangle.lineTo(frame.center().x() + 5.5, frame.center().y())
        triangle.closeSubpath()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(tokens["accent_text"]))
        painter.drawPath(triangle)

        painter.setFont(self._title_font)
        painter.setPen(QColor(tokens["text"]))
        painter.drawText(QRectF(rect.left(), center.y() - 6, rect.width(), 22), Qt.AlignCenter, tr(self.empty_title_key))
        painter.setFont(self._hint_font)
        painter.setPen(QColor(tokens["muted"]))
        painter.drawText(
            QRectF(rect.left() + 12, center.y() + 16, rect.width() - 24, 20),
            Qt.AlignCenter,
            tr("files.empty.hint"),
        )


class PathField(QWidget):
    """Export destination: folder name first, the full path elided in the middle."""

    def __init__(self, placeholder_key: str = "output.none", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.placeholder_key = placeholder_key
        self.path: Path | None = None
        self.tokens = THEMES["light"]
        self._name_font = ui_font(13, QFont.DemiBold)
        self._path_font = ui_font(12, QFont.Medium)
        self.setFixedHeight(42)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumWidth(120)

    def set_path(self, path: Path | None) -> None:
        self.path = path
        self.setToolTip(str(path) if path else "")
        self.update()

    def text(self) -> str:
        return compact_path(self.path) if self.path else tr(self.placeholder_key)

    def set_theme(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        tokens = self.tokens
        rect = QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75)
        radius = rect.height() / 2
        if self.path:
            painter.setPen(QPen(QColor(tokens["field_border"]), 1))
            painter.setBrush(QColor(tokens["field"]))
        else:
            dashed = QPen(QColor(tokens["faint"]), 1.2)
            dashed.setStyle(Qt.DashLine)
            painter.setPen(dashed)
            painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)

        icon_color = QColor(tokens["accent_text"] if self.path else tokens["faint"])
        folder = QPainterPath()
        left, top = rect.left() + 17, rect.center().y() - 6.5
        folder.moveTo(left, top + 13)
        folder.lineTo(left, top)
        folder.lineTo(left + 6, top)
        folder.lineTo(left + 8, top + 2.5)
        folder.lineTo(left + 17, top + 2.5)
        folder.lineTo(left + 17, top + 13)
        folder.closeSubpath()
        icon_pen = QPen(icon_color, 1.6)
        icon_pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(icon_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(folder)

        x = rect.left() + 46
        right = rect.right() - 16
        center_y = rect.center().y()
        name_metrics = QFontMetricsF(self._name_font)
        path_metrics = QFontMetricsF(self._path_font)
        if not self.path:
            painter.setFont(self._name_font)
            painter.setPen(QColor(tokens["muted"]))
            painter.drawText(QPointF(x, center_y + name_metrics.capHeight() / 2), tr(self.placeholder_key))
            return

        name = self.path.name or str(self.path)
        name_text = name_metrics.elidedText(name, Qt.ElideMiddle, (right - x) * 0.55)
        painter.setFont(self._name_font)
        painter.setPen(QColor(tokens["text"]))
        painter.drawText(QPointF(x, center_y + name_metrics.capHeight() / 2), name_text)
        x += name_metrics.horizontalAdvance(name_text) + 12
        full = path_metrics.elidedText(compact_path(self.path, 200), Qt.ElideMiddle, max(0.0, right - x))
        painter.setFont(self._path_font)
        painter.setPen(QColor(tokens["muted"]))
        painter.drawText(QPointF(x, center_y + path_metrics.capHeight() / 2), full)


class ChipSelector(QWidget):
    """Row of toggle chips with a caption, e.g. the photo processing strength."""

    currentChanged = Signal(str)

    def __init__(self, options: tuple, captions: dict | None = None, parent: QWidget | None = None) -> None:
        """``options``: ((key, label_i18n_key), ...); ``captions``: {key: caption_i18n_key}."""
        super().__init__(parent)
        self.captions = captions or {}
        self.labels = dict(options)
        self.current = options[0][0]
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.chips: dict[str, PillButton] = {}
        for key, label_key in options:
            chip = PillButton(tr(label_key), "chip")
            chip.setFont(ui_font(13, QFont.DemiBold))
            chip.clicked.connect(lambda _checked=False, selected=key: self.set_current(selected))
            self.chips[key] = chip
            layout.addWidget(chip)
        layout.addSpacing(8)
        self.caption = ElidedLabel(ui_font(12, QFont.Medium), elide=Qt.ElideNone)
        layout.addWidget(self.caption, 1)
        self.set_current(self.current, emit=False)

    def set_current(self, key: str, emit: bool = True) -> None:
        self.current = key
        for chip_key, chip in self.chips.items():
            chip.setChecked(chip_key == key)
        self.caption.setText(tr(self.captions[key]) if key in self.captions else "")
        if emit:
            self.currentChanged.emit(key)

    def retranslate_ui(self) -> None:
        for key, chip in self.chips.items():
            chip.setText(tr(self.labels[key]))
            chip.updateGeometry()
        self.set_current(self.current, emit=False)

    def set_theme(self, tokens: dict[str, str]) -> None:
        for chip in self.chips.values():
            chip.set_theme(tokens)
        self.caption.set_theme(tokens)


class ProfileField(QWidget):
    """Pill showing the active metadata profile: name first, origin in muted text."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = ""
        self.detail = ""
        self.neutral = True
        self.tokens = THEMES["light"]
        self._name_font = ui_font(13, QFont.DemiBold)
        self._detail_font = ui_font(12, QFont.Medium)
        self.setFixedHeight(42)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumWidth(120)

    def set_profile(self, name: str, detail: str, neutral: bool) -> None:
        self.name, self.detail, self.neutral = name, detail, neutral
        self.setToolTip(f"{name}\n{detail}" if detail else name)
        self.update()

    def text(self) -> str:
        return self.name

    def set_theme(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        tokens = self.tokens
        rect = QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75)
        radius = rect.height() / 2
        painter.setPen(QPen(QColor(tokens["field_border"]), 1))
        painter.setBrush(QColor(tokens["field"]))
        painter.drawRoundedRect(rect, radius, radius)
        center_y = rect.center().y()
        # Tag glyph: lime dot when a real profile is active.
        painter.setPen(QPen(QColor(tokens["text"] if not self.neutral else tokens["faint"]), 1.2))
        painter.setBrush(QColor(BRAND_LIME) if not self.neutral else Qt.NoBrush)
        painter.drawEllipse(QPointF(rect.left() + 21, center_y), 4.5, 4.5)
        x = rect.left() + 36
        right = rect.right() - 16
        name_metrics = QFontMetricsF(self._name_font)
        detail_metrics = QFontMetricsF(self._detail_font)
        name = name_metrics.elidedText(self.name, Qt.ElideRight, (right - x) * 0.65)
        painter.setFont(self._name_font)
        painter.setPen(QColor(tokens["text"]))
        painter.drawText(QPointF(x, center_y + name_metrics.capHeight() / 2), name)
        x += name_metrics.horizontalAdvance(name) + 12
        detail = detail_metrics.elidedText(self.detail, Qt.ElideRight, max(0.0, right - x))
        painter.setFont(self._detail_font)
        painter.setPen(QColor(tokens["muted"]))
        painter.drawText(QPointF(x, center_y + detail_metrics.capHeight() / 2), detail)


class FormatMenu(QWidget):
    selected = Signal(int)
    closed = Signal()

    ROW = 34
    PAD = 8

    def __init__(
        self,
        options: list[tuple[str, str, str]],
        current_index: int,
        tokens: dict[str, str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.options = options
        self.current_index = current_index
        self.hover_index = current_index
        self.tokens = tokens
        self._ratio_font = display_font(12)
        self._caption_font = ui_font(13, QFont.Medium)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setFixedHeight(self.PAD * 2 + self.ROW * len(options))

    def _row_at(self, y: float) -> int | None:
        index = int((y - self.PAD) // self.ROW)
        return index if 0 <= index < len(self.options) and y >= self.PAD else None

    def mouseMoveEvent(self, event) -> None:
        index = self._row_at(event.position().y())
        if index is not None and index != self.hover_index:
            self.hover_index = index
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        index = self._row_at(event.position().y())
        if index is not None and self.rect().contains(event.position().toPoint()):
            self.selected.emit(index)
            self.close()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Down:
            self.hover_index = min(len(self.options) - 1, self.hover_index + 1)
            self.update()
        elif event.key() == Qt.Key_Up:
            self.hover_index = max(0, self.hover_index - 1)
            self.update()
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.selected.emit(self.hover_index)
            self.close()
        else:
            super().keyPressEvent(event)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.closed.emit()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        tokens = self.tokens
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(tokens["menu_border"]), 1))
        painter.setBrush(QColor(tokens["menu_bg"]))
        painter.drawRoundedRect(rect, 20, 20)

        ratio_metrics = QFontMetricsF(self._ratio_font)
        caption_metrics = QFontMetricsF(self._caption_font)
        caption_x = 32 + max(ratio_metrics.horizontalAdvance(short) for _n, short, _c in self.options) + 18
        for index, (_name, short, caption_key) in enumerate(self.options):
            caption = caption_metrics.elidedText(tr(caption_key), Qt.ElideRight, rect.width() - 12 - caption_x - 12)
            row = QRectF(6, self.PAD + index * self.ROW, rect.width() - 12, self.ROW)
            selected = index == self.current_index
            if index == self.hover_index:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(tokens["menu_hover"]))
                painter.drawRoundedRect(row, 14, 14)
            center_y = row.center().y()
            if selected:
                painter.setPen(QPen(QColor(tokens["text"]), 1.2))
                painter.setBrush(QColor(BRAND_LIME))
                painter.drawEllipse(QPointF(row.left() + 16, center_y), 4, 4)
            painter.setFont(self._ratio_font)
            painter.setPen(QColor(tokens["accent_text"] if selected else tokens["text"]))
            painter.drawText(QPointF(row.left() + 32, center_y + ratio_metrics.capHeight() / 2), short)
            painter.setFont(self._caption_font)
            painter.setPen(QColor(tokens["text"] if selected else tokens["muted"]))
            painter.drawText(QPointF(row.left() + caption_x, center_y + caption_metrics.capHeight() / 2), caption)


class FormatSelector(QWidget):
    """Quick chips for the popular ratios plus "More" with the full list."""

    currentIndexChanged = Signal(int)

    QUICK = ("9:16", "16:9", "1:1", "4:5")

    def __init__(self, options: list[tuple[str, str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.options = options
        self.current_index = 0
        self.tokens = THEMES["light"]
        self.menu: FormatMenu | None = None
        self._menu_closed_at = 0.0
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        names = [name for name, _short, _caption in options]
        self.chips: list[tuple[int, PillButton]] = []
        for name in self.QUICK:
            index = names.index(name)
            chip = PillButton(options[index][1], "chip")
            chip.clicked.connect(lambda _checked=False, selected=index: self.setCurrentIndex(selected))
            self.chips.append((index, chip))
            layout.addWidget(chip)
        self.more = PillButton(tr("format.more"), "chip", chevron=True)
        self.more.clicked.connect(self._toggle_menu)
        layout.addWidget(self.more)
        layout.addSpacing(8)
        self.caption = ElidedLabel(ui_font(12, QFont.Medium), elide=Qt.ElideNone)
        layout.addWidget(self.caption, 1)
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        for index, chip in self.chips:
            chip.setToolTip(f"{self.options[index][1]} · {tr(self.options[index][2])}")
        self.more.setToolTip(tr("format.all"))
        self._sync()

    def buttons(self) -> list[PillButton]:
        return [chip for _index, chip in self.chips] + [self.more]

    def currentData(self) -> str:
        return self.options[self.current_index][0]

    def setCurrentIndex(self, index: int) -> None:
        if not 0 <= index < len(self.options):
            return
        self.current_index = index
        self._sync()
        self.currentIndexChanged.emit(index)

    def set_theme(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens
        for button in self.buttons():
            button.set_theme(tokens)
        self.caption.set_theme(tokens)

    def _sync(self) -> None:
        quick = False
        for index, chip in self.chips:
            chip.setChecked(index == self.current_index)
            quick = quick or index == self.current_index
        _name, short, caption_key = self.options[self.current_index]
        self.more.setText(tr("format.more") if quick else short)
        self.more.setChecked(not quick)
        self.more.updateGeometry()
        random_crop = self.current_index == len(self.options) - 1
        self.caption.setText(tr(caption_key))
        self.caption.setToolTip(tr("format.random_tooltip") if random_crop else tr("format.center_tooltip", ratio=short))

    def _mark_menu_closed(self) -> None:
        self._menu_closed_at = time.monotonic()
        self._sync()

    def _toggle_menu(self) -> None:
        # A click on the button while the popup is open first closes the popup;
        # don't immediately reopen it.
        if time.monotonic() - self._menu_closed_at < 0.25:
            self._sync()
            return
        self.menu = FormatMenu(self.options, self.current_index, self.tokens, self)
        self.menu.selected.connect(self.setCurrentIndex)
        self.menu.closed.connect(self._mark_menu_closed)
        self.menu.setFixedWidth(300)

        position = self.more.mapToGlobal(QPoint(0, self.more.height() + 8))
        screen = QApplication.screenAt(position) or self.screen()
        if screen:
            available = screen.availableGeometry()
            if position.y() + self.menu.height() > available.bottom() - 12:
                position.setY(self.more.mapToGlobal(QPoint(0, 0)).y() - self.menu.height() - 8)
            position.setX(min(position.x(), available.right() - self.menu.width() - 12))
        self.menu.move(position)
        self.menu.show()
        self.menu.setFocus()


class LogHeader(QWidget):
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.expanded = False
        self.count = 0
        self.errors = 0
        self.preview = ""
        self.tokens = THEMES["light"]
        self._title_font = ui_font(13, QFont.DemiBold)
        self._meta_font = ui_font(12, QFont.Medium)
        self._badge_font = display_font(10, 125)
        self.setFixedHeight(44)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self.setToolTip(tr("logs.toggle_tooltip"))
        self.setAccessibleName(tr("logs.title"))
        self.update()

    def set_theme(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        tokens = self.tokens
        rect = QRectF(self.rect())
        center_y = rect.center().y()
        if self.underMouse():
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(tokens["outline_hover"]))
            painter.drawRoundedRect(rect, 18, 18)

        chevron_pen = QPen(QColor(tokens["text"]), 1.7)
        chevron_pen.setCapStyle(Qt.RoundCap)
        chevron_pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(chevron_pen)
        painter.setBrush(Qt.NoBrush)
        chevron = QPainterPath()
        cx = rect.left() + 22
        if self.expanded:
            chevron.moveTo(cx - 4, center_y - 2)
            chevron.lineTo(cx, center_y + 2)
            chevron.lineTo(cx + 4, center_y - 2)
        else:
            chevron.moveTo(cx - 2, center_y - 4)
            chevron.lineTo(cx + 2, center_y)
            chevron.lineTo(cx - 2, center_y + 4)
        painter.drawPath(chevron)

        title_metrics = QFontMetricsF(self._title_font)
        meta_metrics = QFontMetricsF(self._meta_font)
        x = rect.left() + 38
        painter.setFont(self._title_font)
        painter.setPen(QColor(tokens["text"]))
        title = tr("logs.title")
        painter.drawText(QPointF(x, center_y + title_metrics.capHeight() / 2), title)
        x += title_metrics.horizontalAdvance(title) + 10

        badge_text = str(self.count)
        badge_metrics = QFontMetricsF(self._badge_font)
        badge = QRectF(x, center_y - 10, max(26.0, badge_metrics.horizontalAdvance(badge_text) + 16), 20)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(tokens["field"]))
        painter.drawRoundedRect(badge, 10, 10)
        painter.setFont(self._badge_font)
        painter.setPen(QColor(tokens["accent_text"]))
        painter.drawText(badge, Qt.AlignCenter, badge_text)
        x = badge.right() + 8
        if self.errors:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(BRAND_ERROR))
            painter.drawEllipse(QPointF(x + 4, center_y), 4, 4)
            x += 16

        action = tr("logs.hide") if self.expanded else tr("logs.show")
        action_width = meta_metrics.horizontalAdvance(action)
        right = rect.right() - 20
        painter.setFont(self._meta_font)
        painter.setPen(QColor(tokens["accent_text"]))
        painter.drawText(QPointF(right - action_width, center_y + meta_metrics.capHeight() / 2), action)

        preview_width = right - action_width - 24 - x
        if self.preview and preview_width > 40:
            painter.setPen(QColor(tokens["muted"]))
            preview = meta_metrics.elidedText(self.preview, Qt.ElideRight, preview_width)
            painter.drawText(QPointF(x + 6, center_y + meta_metrics.capHeight() / 2), preview)


class LogPanel(QFrame):
    """Collapsible log; it keeps collecting lines while collapsed."""

    toggled = Signal(bool)

    def __init__(self, log_box: QPlainTextEdit, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LogPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = LogHeader()
        self.header.clicked.connect(self.toggle)
        layout.addWidget(self.header)
        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(12, 0, 12, 12)
        self.log_box = log_box
        body_layout.addWidget(log_box)
        layout.addWidget(self.body)
        self.body.hide()

    def is_expanded(self) -> bool:
        return self.header.expanded

    def toggle(self) -> None:
        self.set_expanded(not self.header.expanded)

    def set_expanded(self, expanded: bool) -> None:
        self.header.expanded = expanded
        self.body.setVisible(expanded)
        self.header.update()
        self.toggled.emit(expanded)

    def note(self, line: str, is_error: bool) -> None:
        self.header.count += 1
        self.header.errors += int(is_error)
        self.header.preview = line
        self.header.update()

    def set_theme(self, tokens: dict[str, str]) -> None:
        self.header.set_theme(tokens)


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return

    try:
        process.terminate()
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    except Exception:
        pass


def run_ffmpeg(
    input_path: Path,
    output_path: Path,
    aspect_name: str,
    stop_event: threading.Event | None = None,
    process_callback: Callable[[subprocess.Popen | None], None] | None = None,
    encode_options: dict | None = None,
    keep_metadata: bool = False,
) -> tuple[float, float]:
    # encode_options come from a metadata profile (media_metadata.ffmpeg_video_options);
    # without them the command is exactly the original one.
    encode_options = encode_options or {}
    duration = probe_duration(input_path)
    trim_start = random.uniform(0.05, 0.15)
    trim_end = random.uniform(0.05, 0.15)
    output_duration = duration - trim_start - trim_end

    if output_duration <= 0.5:
        trim_start = 0
        trim_end = 0
        output_duration = duration

    command = [
        get_ffmpeg_path(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        # Output seeking (after -i): frame-accurate and robust for every container. Seeking on
        # the input side yields an empty file for H.264 in AVI; the trim is only 0.05-0.15 s.
        "-ss",
        f"{trim_start:.3f}",
        "-t",
        f"{output_duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-sn",
        "-dn",
        "-map_metadata",
        "0" if keep_metadata else "-1",
        "-vf",
        build_filter_chain(aspect_name),
        *(encode_options.get("video_args") or ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        *encode_options.get("audio_args", []),
        "-movflags",
        "+faststart",
        *encode_options.get("mux_args", []),
        str(output_path),
    ]

    process = popen_process(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if process_callback:
        process_callback(process)

    try:
        while True:
            if stop_event and stop_event.is_set():
                terminate_process(process)
                raise ProcessingStopped(tr("error.stopped"))

            return_code = process.poll()
            if return_code is not None:
                break

            time.sleep(0.1)

        _stdout, stderr = process.communicate()
    finally:
        if process_callback:
            process_callback(None)

    if return_code != 0:
        message = stderr.strip() or tr("error.ffmpeg_failed")
        raise RuntimeError(message)
    if not has_video_stream(output_path):
        raise RuntimeError(tr("error.no_video_output"))

    return trim_start, trim_end


def has_video_stream(path: Path) -> bool:
    """FFmpeg can exit successfully without writing a single frame; never report that as done."""
    try:
        result = run_process([get_ffmpeg_path(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return "Video:" in (result.stderr or "")


def write_error_log(exc: BaseException) -> Path:
    log_path = app_data_dir() / "startup_error.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        encoding="utf-8",
    )
    return log_path


def compact_path(path: Path, max_chars: int = 38) -> str:
    text = str(path)
    home = str(Path.home())
    if text == home or text.startswith(home + os.sep):
        text = "~" + text[len(home):]
    if len(text) <= max_chars:
        return text
    return f".../{path.parent.name}/{path.name}"


def compact_name(name: str, max_chars: int = 30) -> str:
    if len(name) <= max_chars:
        return name
    return f"{name[:max_chars - 10]}…{name[-9:]}"


class ThemeReveal(QWidget):
    """Circular theme transition centred on the window.

    The live interface already shows the new theme; this overlay paints a snapshot of the
    previous theme through a circular mask. Going dark, the new theme expands from the centre
    (the old one stays outside a growing circle); going light, the old dark theme stays inside
    a circle that collapses into the centre.
    """

    DURATION = 1200
    FEATHER = 4.0

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.snapshot: QPixmap | None = None
        self.expand = True
        self.keep_live: list[QWidget] = []
        self._progress = 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(self.DURATION)
        self._animation.setEasingCurve(QEasingCurve.InOutCubic)
        self._animation.valueChanged.connect(self._step)
        self._animation.finished.connect(self.stop)
        self.hide()

    def is_running(self) -> bool:
        return self._animation.state() == QVariantAnimation.Running

    def start(self, snapshot: QPixmap, expand: bool) -> None:
        self._animation.stop()
        self.snapshot = snapshot
        self.expand = expand
        self._progress = 0.0
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self._animation.start()

    def stop(self) -> None:
        self._animation.stop()
        self.hide()
        self.snapshot = None

    def _step(self, value: float) -> None:
        self._progress = float(value)
        self.update()

    def radius(self, progress: float) -> float:
        # Far enough to clear every corner (plus the soft edge) at any aspect ratio.
        full = math.hypot(self.width() / 2, self.height() / 2) + self.FEATHER * 2
        return full * (progress if self.expand else 1 - progress)

    def _old_theme_area(self, radius: float) -> QPainterPath:
        circle = QPainterPath()
        if radius > 0:
            circle.addEllipse(QPointF(self.width() / 2, self.height() / 2), radius, radius)
        if not self.expand:
            return circle
        area = QPainterPath()
        area.addRect(QRectF(self.rect()))
        return area.subtracted(circle)

    def paintEvent(self, event) -> None:
        if self.snapshot is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        live = QPainterPath()
        for widget in self.keep_live:
            # Theme-independent controls (the animated switch) always stay live.
            if widget.isVisible():
                live.addRect(QRectF(widget.mapTo(self.parentWidget(), QPoint(0, 0)), QSize(widget.size())))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(self.snapshot))
        radius = self.radius(self._progress)
        # A few px of soft edge: the old theme fades out across the boundary.
        toward_new = self.FEATHER if not self.expand else -self.FEATHER
        for offset, opacity in ((toward_new, 0.22), (toward_new / 2, 0.45), (0.0, 1.0)):
            painter.setOpacity(opacity)
            painter.drawPath(self._old_theme_area(max(0.0, radius + offset)).subtracted(live))


class MetadataPage(QFrame):
    """Metadata manager shown inside the main window as a full page: presets, manual editing, sources."""

    closed = Signal()

    TIME_MODES = (("now", "meta.page.time.now"), ("source", "meta.page.time.source"), ("custom", "meta.page.time.custom"))
    COLUMNS = ("meta.page.col.group", "meta.page.col.field", "meta.page.col.value", "meta.page.col.status")
    SOURCE_COLUMNS = ("meta.page.src.tag", "meta.page.src.value", "meta.page.src.importable")
    TABS = ("meta.page.tab.fields", "meta.page.tab.manual", "meta.page.tab.sources")

    def __init__(self, store: ProfileStore, media: str, tokens: dict[str, str], parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("MetadataPage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.store = store
        self.media = media
        self.tokens = tokens
        self.themed: list[QWidget] = []
        self.profile: dict | None = None  # working copy being edited
        self.dirty = False
        self.applied: dict[str, str] = {}  # media -> applied profile id
        self._loading = False
        self.source_meta: dict = {}
        self.source_name = ""
        self._texts: list[tuple[Callable[[str], None], str]] = []
        self._status: tuple[str, dict] | None = None

        self._slide = QVariantAnimation(self)
        self._slide.setDuration(220)
        self._slide.setEasingCurve(QEasingCurve.OutCubic)
        self._slide.valueChanged.connect(lambda offset: self.move(0, int(offset)))

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        top = QHBoxLayout()
        page_title = self._t(DisplayLabel("", 20))
        self._tx(lambda text, w=page_title: w.set_text(text.upper()), "meta.page.title")
        top.addWidget(page_title, 0, Qt.AlignVCenter)
        top.addSpacing(16)
        media_track = CobaltPanel()
        media_track.setFixedHeight(48)
        track_layout = QHBoxLayout(media_track)
        track_layout.setContentsMargins(4, 4, 4, 4)
        self.media_switch = ThemeSwitch(options=(("video", "mode.video"), ("photo", "mode.photo")))
        for key, button in self.media_switch.buttons.items():
            button.clicked.connect(lambda _c=False, selected=key: self._switch_media(selected))
        track_layout.addWidget(self.media_switch)
        top.addWidget(media_track, 0, Qt.AlignVCenter)
        top.addStretch(1)
        close = self._t(PillButton("", "outline"))
        self._tx(close.setText, "common.close")
        self._tx(close.setToolTip, "meta.page.close_tooltip")
        close.clicked.connect(self.close_page)
        top.addWidget(close)
        root.addLayout(top)

        body = QHBoxLayout()
        body.setSpacing(18)
        sidebar = QFrame()
        sidebar.setObjectName("Card")
        sidebar.setFixedWidth(272)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(14, 16, 14, 14)
        side_layout.setSpacing(10)
        profiles_title = self._t(DisplayLabel("", 12, "muted"))
        self._tx(lambda text, w=profiles_title: w.set_text(text.upper()), "meta.page.profiles")
        side_layout.addWidget(profiles_title)
        self.profile_list = QListWidget()
        self.profile_list.setObjectName("ProfileList")
        self.profile_list.setFocusPolicy(Qt.NoFocus)
        self.profile_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.profile_list.setTextElideMode(Qt.ElideRight)
        self.profile_list.currentItemChanged.connect(self._on_profile_selected)
        side_layout.addWidget(self.profile_list, 1)
        body.addWidget(sidebar)

        detail = QFrame()
        detail.setObjectName("Card")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(22, 18, 22, 18)
        detail_layout.setSpacing(10)
        self.title_label = self._t(DisplayLabel("—", 18))
        detail_layout.addWidget(self.title_label)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("DialogMuted")
        self.summary_label.setWordWrap(True)
        self.summary_label.setFont(ui_font(13, QFont.Medium))
        detail_layout.addWidget(self.summary_label)

        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self.tab_buttons = []
        for index, key in enumerate(self.TABS):
            chip = self._t(PillButton("", "chip"))
            self._tx(lambda text, w=chip: (w.setText(text), w.updateGeometry()), key)
            chip.setFont(ui_font(13, QFont.DemiBold))
            chip.clicked.connect(lambda _c=False, i=index: self._show_tab(i))
            self.tab_buttons.append(chip)
            tabs.addWidget(chip)
        tabs.addStretch(1)
        detail_layout.addLayout(tabs)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._fields_page())
        self.pages.addWidget(self._manual_page())
        self.pages.addWidget(self._sources_page())
        detail_layout.addWidget(self.pages, 1)
        body.addWidget(detail, 1)
        root.addLayout(body, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.status_label = QLabel()
        self.status_label.setObjectName("DialogMuted")
        self.status_label.setFont(ui_font(12, QFont.Medium))
        bottom.addWidget(self.status_label, 1)
        self.reset_button = self._t(PillButton("", "outline"))
        self.reset_button.clicked.connect(self._reset_builtin)
        self.save_as_button = self._t(PillButton("", "outline"))
        self.save_as_button.clicked.connect(self._save_as)
        self.delete_button = self._t(PillButton("", "outline"))
        self.delete_button.clicked.connect(self._delete_custom)
        self.apply_button = self._t(PillButton(""))
        self.apply_button.clicked.connect(self._apply)
        for button, key in ((self.reset_button, "meta.page.reset"), (self.save_as_button, "meta.page.save_as"),
                            (self.delete_button, "meta.page.delete"), (self.apply_button, "meta.page.apply")):
            self._tx(lambda text, w=button: (w.setText(text), w.updateGeometry()), key)
            bottom.addWidget(button)
        root.addLayout(bottom)

        for widget in self.themed:
            widget.set_theme(tokens)
        self.media_switch.set_mode(media, animate=False)
        self._show_tab(0)
        self._fill_list(select=store.active_id(media))

    # ---------------------------------------------------------------- building blocks

    def _t(self, widget: QWidget) -> QWidget:
        self.themed.append(widget)
        return widget

    def _tx(self, setter: Callable[[str], None], key: str) -> None:
        """Register a static text so the page can be retranslated live."""
        self._texts.append((setter, key))
        setter(tr(key))

    def _set_status(self, key: str | None, /, **params) -> None:
        self._status = (key, params) if key else None
        self.status_label.setText(self._status_text_now())

    def _status_text_now(self) -> str:
        if not self._status:
            return ""
        key, params = self._status
        resolved = {name: (value() if callable(value) else value) for name, value in params.items()}
        return tr(key, **resolved)

    def retranslate_ui(self) -> None:
        for setter, key in self._texts:
            setter(tr(key))
        self.media_switch.retranslate_ui()
        self._loading = True
        for combo, entries in ((self.time_mode, self.TIME_MODES),):
            for index, (_key, label_key) in enumerate(entries):
                combo.setItemText(index, tr(label_key))
        self.keep_source.setItemText(0, tr("meta.page.keep.remove"))
        self.keep_source.setItemText(1, tr("meta.page.keep.keep"))
        self._loading = False
        self.fields_table.setHorizontalHeaderLabels([tr(key) for key in self.COLUMNS])
        self.source_table.setHorizontalHeaderLabels([tr(key) for key in self.SOURCE_COLUMNS])
        if self.source_meta:
            self.source_label.setText(tr("meta.page.source_loaded", name=self.source_name, n=len(self.source_meta)))
            self._fill_source_table()
        self._fill_list(select=self.profile["id"] if self.profile else None, keep_selection=True)
        if self.profile:
            self._render_profile()
        self.status_label.setText(self._status_text_now())

    def _fields_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(10)
        options = QHBoxLayout()
        options.setSpacing(8)
        options.addWidget(self._muted_key("meta.page.time_label"))
        self.time_mode = QComboBox()
        for key, label_key in self.TIME_MODES:
            self.time_mode.addItem(tr(label_key), key)
        self.time_mode.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.time_mode.currentIndexChanged.connect(self._time_settings_changed)
        options.addWidget(self.time_mode)
        self.time_custom = QLineEdit()
        self._tx(self.time_custom.setPlaceholderText, "meta.page.datetime_placeholder")
        self.time_custom.setFixedWidth(170)
        self.time_custom.editingFinished.connect(self._time_settings_changed)
        options.addWidget(self.time_custom)
        options.addWidget(self._muted_key("meta.page.offset_label"))
        self.time_offset = QLineEdit()
        self._tx(self.time_offset.setPlaceholderText, "meta.page.offset_placeholder")
        self._tx(self.time_offset.setToolTip, "meta.page.offset_tooltip")
        self.time_offset.setFixedWidth(110)
        self.time_offset.editingFinished.connect(self._time_settings_changed)
        options.addWidget(self.time_offset)
        options.addStretch(1)
        layout.addLayout(options)
        # Second row, so nothing gets squeezed at the minimum window width.
        options = QHBoxLayout()
        options.setSpacing(8)
        options.addWidget(self._muted_key("meta.page.keep_label"))
        self.keep_source = QComboBox()
        self.keep_source.addItem(tr("meta.page.keep.remove"), False)
        self.keep_source.addItem(tr("meta.page.keep.keep"), True)
        self._tx(self.keep_source.setToolTip, "meta.page.keep_tooltip")
        self.keep_source.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.keep_source.currentIndexChanged.connect(self._time_settings_changed)
        options.addWidget(self.keep_source)
        options.addStretch(1)
        layout.addLayout(options)

        self.fields_table = QTableWidget(0, len(self.COLUMNS))
        self.fields_table.setObjectName("MetaTable")
        self.fields_table.setHorizontalHeaderLabels([tr(key) for key in self.COLUMNS])
        self.fields_table.verticalHeader().setVisible(False)
        self.fields_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.fields_table.setSelectionMode(QAbstractItemView.SingleSelection)
        header = self.fields_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        header.setMinimumSectionSize(60)
        self.fields_table.setColumnWidth(1, 200)
        self.fields_table.setColumnWidth(3, 170)
        self.fields_table.setTextElideMode(Qt.ElideRight)
        self.fields_table.itemChanged.connect(self._field_edited)
        layout.addWidget(self.fields_table, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.add_field_combo = QComboBox()
        self.add_field_combo.setMinimumWidth(180)
        self.add_field_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.add_field_combo.setMinimumContentsLength(14)
        actions.addWidget(self.add_field_combo, 1)
        self.add_field_button = self._t(PillButton("", "outline"))
        self._tx(lambda text: (self.add_field_button.setText(text), self.add_field_button.updateGeometry()), "meta.page.add_field")
        self.add_field_button.clicked.connect(self._add_field)
        actions.addWidget(self.add_field_button)
        self.remove_field_button = self._t(PillButton("", "outline"))
        self._tx(lambda text: (self.remove_field_button.setText(text), self.remove_field_button.updateGeometry()),
                 "meta.page.remove_field")
        self.remove_field_button.clicked.connect(self._remove_field)
        actions.addWidget(self.remove_field_button)
        layout.addLayout(actions)
        return page

    def _manual_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(10)
        row = QHBoxLayout()
        row.setSpacing(8)
        pick = self._t(PillButton("", "outline"))
        self._tx(lambda text: (pick.setText(text), pick.updateGeometry()), "meta.page.pick_file")
        pick.clicked.connect(self._pick_source_file)
        row.addWidget(pick)
        self.source_label = self._muted(tr("meta.page.source_hint"))
        self.source_label.setWordWrap(True)
        row.addWidget(self.source_label, 1)
        self.search = QLineEdit()
        self._tx(self.search.setPlaceholderText, "meta.page.search")
        self.search.setFixedWidth(220)
        self.search.textChanged.connect(self._filter_source_table)
        row.addWidget(self.search)
        layout.addLayout(row)
        self.source_table = QTableWidget(0, 3)
        self.source_table.setObjectName("MetaTable")
        self.source_table.setHorizontalHeaderLabels([tr(key) for key in self.SOURCE_COLUMNS])
        self.source_table.verticalHeader().setVisible(False)
        self.source_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.source_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        header = self.source_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.source_table.setColumnWidth(0, 300)
        layout.addWidget(self.source_table, 1)
        actions = QHBoxLayout()
        self.import_button = self._t(PillButton("", "outline"))
        self._tx(lambda text: (self.import_button.setText(text), self.import_button.updateGeometry()), "meta.page.import")
        self.import_button.clicked.connect(self._import_checked)
        actions.addWidget(self.import_button)
        import_hint = self._muted_key("meta.page.import_hint")
        import_hint.setWordWrap(True)
        actions.addWidget(import_hint, 1)
        layout.addLayout(actions)
        return page

    def _sources_page(self) -> QWidget:
        self.sources_view = QTextBrowser()
        self.sources_view.setObjectName("MetaText")
        self.sources_view.setOpenExternalLinks(True)
        return self.sources_view

    def _muted(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("DialogMuted")
        label.setFont(ui_font(12, QFont.Medium))
        return label

    def _muted_key(self, key: str) -> QLabel:
        label = self._muted("")
        self._tx(label.setText, key)
        return label

    def _show_tab(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        for i, button in enumerate(self.tab_buttons):
            button.setChecked(i == index)

    # ---------------------------------------------------------------- profile list

    def _fill_list(self, select: str | None = None, keep_selection: bool = False) -> None:
        self._loading = True
        self.profile_list.clear()
        groups: dict[str, list[dict]] = {}
        for profile in self.store.profiles(self.media):
            groups.setdefault(profile_group(profile), []).append(profile)
        target_row = None
        for group, profiles in groups.items():
            header = QListWidgetItem(group.upper())
            header.setFlags(Qt.NoItemFlags)
            header.setFont(display_font(10, 125))
            header.setForeground(QColor(self.tokens["muted"]))
            self.profile_list.addItem(header)
            for profile in profiles:
                label = profile_name(profile)
                if profile.get("modified"):
                    label += "  · " + tr("meta.page.badge.modified_short")
                if not profile.get("available", True):
                    label += "  · " + tr("meta.page.badge.unavailable")
                item = QListWidgetItem("   " + label)
                item.setData(Qt.UserRole, profile["id"])
                item.setFont(ui_font(13, QFont.DemiBold if profile["id"] == self.store.active_id(self.media) else QFont.Medium))
                if not profile.get("available", True):
                    item.setForeground(QColor(self.tokens["faint"]))
                item.setToolTip(profile_summary(profile))
                self.profile_list.addItem(item)
                if profile["id"] == select:
                    target_row = self.profile_list.count() - 1
        if keep_selection:
            # Retranslation only: keep the working copy (and its unsaved edits) untouched.
            self.profile_list.setCurrentRow(target_row if target_row is not None else 1)
            self._loading = False
            return
        self._loading = False
        self.profile_list.setCurrentRow(target_row if target_row is not None else 1)

    def _on_profile_selected(self, current, _previous) -> None:
        if self._loading or current is None or not current.data(Qt.UserRole):
            return
        if self.dirty and self.profile and not self._confirm_discard():
            return
        self._load_profile(self.store.get(current.data(Qt.UserRole)))

    def _confirm_discard(self) -> bool:
        answer = QMessageBox.question(self, tr("meta.page.discard_title"), tr("meta.page.discard_text"))
        return answer == QMessageBox.Yes

    def _switch_media(self, media: str) -> None:
        if media == self.media:
            return
        if self.dirty and not self._confirm_discard():
            self.media_switch.set_mode(self.media)
            return
        self.media = media
        self.dirty = False
        self.media_switch.set_mode(media)
        self._fill_list(select=self.store.active_id(media))

    # ---------------------------------------------------------------- profile editing

    def _load_profile(self, profile: dict) -> None:
        self.profile = profile
        self.dirty = False
        self._loading = True
        self._render_profile()
        times = profile.get("timestamps", {})
        self.time_mode.setCurrentIndex(max(0, [k for k, _ in self.TIME_MODES].index(times.get("mode", "now"))))
        self.time_custom.setText(times.get("custom", ""))
        self.time_offset.setText(times.get("offset", ""))
        self.keep_source.setCurrentIndex(1 if profile.get("keep_source_metadata") else 0)
        self._fill_fields()
        self._fill_sources()
        self._loading = False
        self._update_buttons()
        self._set_status(None)

    def _render_profile(self) -> None:
        """Texts that depend on the language: title, summary and badges, fields, sources."""
        profile = self.profile or {}
        name = profile_name(profile)
        self.title_label.set_text(name.upper() if len(name) < 34 else name)
        badges = []
        if profile.get("builtin"):
            badges.append(tr("meta.page.badge.builtin"))
        else:
            base = tr(profile["based_on_key"]) if profile.get("based_on_key") else profile.get("based_on", "—")
            badges.append(tr("meta.page.badge.custom", base=base))
        if profile.get("modified"):
            badges.append(tr("meta.page.badge.modified"))
        if profile.get("id") and self.store.active_id(self.media) == profile["id"]:
            badges.append(tr("meta.page.badge.active"))
        self.summary_label.setText(f"{profile_summary(profile)}\n{' · '.join(badges)}")
        if not self._loading:
            self._fill_fields()
            self._fill_sources()

    def _fill_fields(self) -> None:
        self._loading = True
        table = self.fields_table
        table.setRowCount(0)
        fields = self.profile.get("fields", {}) if self.profile else {}
        editable = bool(self.profile and self.profile.get("available", True))
        for key, value in fields.items():
            spec = field_spec(self.media, key)
            row = table.rowCount()
            table.insertRow(row)
            group = QTableWidgetItem(spec.group_label if spec else "?")
            label = QTableWidgetItem(spec.label if spec else key)
            label.setToolTip(f"{key}\n{spec.note}" if spec and spec.note else key)
            for item in (group, label):
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            label.setData(Qt.UserRole, key)
            table.setItem(row, 0, group)
            table.setItem(row, 1, label)
            status = QTableWidgetItem(self._status_text(spec, value))
            status.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            table.setItem(row, 3, status)
            if spec and spec.kind in ("enum", "bool") and editable:
                combo = QComboBox()
                choices = (tuple((option, spec.option_label(option)) for option in spec.options) if spec.kind == "enum"
                           else (("1", tr("common.yes")), ("0", tr("common.no"))))
                for option, option_label in choices:
                    combo.addItem(option_label, option)
                index = max(0, [option.strip() for option, _ in choices].index(str(value).strip())) if str(value).strip() in [o.strip() for o, _ in choices] else 0
                combo.setCurrentIndex(index)
                combo.currentIndexChanged.connect(lambda _i, k=key, c=combo: self._set_field(k, c.currentData()))
                table.setCellWidget(row, 2, combo)
                holder = QTableWidgetItem(str(value))
                holder.setFlags(Qt.ItemIsEnabled)
                table.setItem(row, 2, holder)
            else:
                item = QTableWidgetItem(self._display_value(value))
                item.setData(Qt.UserRole, value)
                item.setToolTip(tr("meta.page.value_tooltip"))
                if not editable:
                    item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                table.setItem(row, 2, item)
        table.resizeRowsToContents()
        self._fill_add_combo()
        self._loading = False

    @staticmethod
    def _display_value(value: str) -> str:
        return str(value)

    def _status_text(self, spec, value) -> str:
        if spec is None:
            return tr("meta.page.status.unsupported")
        error = validate_value(spec, value)
        if error:
            return tr("meta.page.status.error", error=error)
        if "$" in str(value):
            return tr("meta.page.status.computed")
        if spec.key.startswith("FFMPEG:"):
            return tr("meta.page.status.container")
        return tr("meta.page.status.written", formats="/".join(fmt.upper() for fmt in spec.formats))

    def _fill_add_combo(self) -> None:
        self.add_field_combo.clear()
        present = set((self.profile or {}).get("fields", {}))
        for spec in FIELDS:
            if spec.media == self.media and spec.key not in present:
                self.add_field_combo.addItem(f"{spec.group_label} · {spec.label}", spec.key)
        enabled = bool(self.profile and self.profile.get("available", True))
        self.add_field_combo.setEnabled(enabled and self.add_field_combo.count() > 0)
        self.add_field_button.setEnabled(enabled and self.add_field_combo.count() > 0)
        self.remove_field_button.setEnabled(enabled and self.fields_table.rowCount() > 0)

    def _field_edited(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != 2 or self.profile is None:
            return
        key = self.fields_table.item(item.row(), 1).data(Qt.UserRole)
        self._set_field(key, item.text().strip(), item)

    def _set_field(self, key: str, value: str, item: QTableWidgetItem | None = None) -> None:
        spec = field_spec(self.media, key)
        error = validate_value(spec, value) if spec else tr("meta.page.field_unsupported")
        if error:
            self._set_status("meta.page.value_rejected", field=lambda: spec.label if spec else key, error=error)
            if item is not None:
                self._loading = True
                item.setText(str(self.profile["fields"].get(key, "")))
                self._loading = False
            return
        self.profile.setdefault("fields", {})[key] = value
        self._mark_dirty("meta.page.change.value", field=lambda: spec.label, value=value)
        for row in range(self.fields_table.rowCount()):
            if self.fields_table.item(row, 1).data(Qt.UserRole) == key:
                self._loading = True
                self.fields_table.item(row, 3).setText(self._status_text(spec, value))
                self._loading = False

    def _add_field(self) -> None:
        key = self.add_field_combo.currentData()
        spec = field_spec(self.media, key)
        if not spec or self.profile is None:
            return
        default = {"enum": spec.options[0] if spec.options else "", "bool": "1", "datetime": "$LOCAL", "offset": "$OFFSET",
                   "subsec": "$SUBSEC", "version": "0232"}.get(spec.kind, "")
        if spec.kind in ("int", "number"):
            default = str(int(spec.minimum)) if spec.minimum and spec.minimum >= 1 else "1"
        self.profile.setdefault("fields", {})[key] = default
        self._mark_dirty("meta.page.change.added", field=lambda: spec.label)
        self._fill_fields()

    def _remove_field(self) -> None:
        row = self.fields_table.currentRow()
        if row < 0 or self.profile is None:
            self._set_status("meta.page.select_row")
            return
        key = self.fields_table.item(row, 1).data(Qt.UserRole)
        self.profile["fields"].pop(key, None)
        self._mark_dirty("meta.page.change.removed", field=key)
        self._fill_fields()

    def _time_settings_changed(self, *_args) -> None:
        if self._loading or self.profile is None:
            return
        mode = self.time_mode.currentData()
        custom = self.time_custom.text().strip()
        offset = self.time_offset.text().strip()
        self.time_custom.setEnabled(mode == "custom")
        if mode == "custom" and custom and not re.fullmatch(r"\d{4}:\d\d:\d\d \d\d:\d\d:\d\d", custom):
            self._set_status("meta.page.time_rejected")
            return
        if offset and not re.fullmatch(r"[+-](0\d|1[0-4]):[0-5]\d", offset):
            self._set_status("meta.page.offset_rejected")
            return
        self.profile["timestamps"] = {"mode": mode, "custom": custom, "offset": offset}
        self.profile["keep_source_metadata"] = bool(self.keep_source.currentData())
        self._mark_dirty("meta.page.change.time")

    def _mark_dirty(self, change_key: str, /, **params) -> None:
        self.dirty = True
        self._set_status("meta.page.changed", change=lambda: tr(change_key, **{
            name: (value() if callable(value) else value) for name, value in params.items()}))
        self._update_buttons()

    def _update_buttons(self) -> None:
        profile = self.profile or {}
        available = profile.get("available", True)
        self.apply_button.setEnabled(bool(profile) and available)
        self.save_as_button.setEnabled(bool(profile) and available)
        self.reset_button.setEnabled(bool(profile.get("builtin")) and (profile.get("modified") or self.dirty))
        self.delete_button.setEnabled(bool(profile) and not profile.get("builtin", True))
        self.time_custom.setEnabled(self.time_mode.currentData() == "custom")
        for widget in (self.time_mode, self.time_offset, self.keep_source):
            widget.setEnabled(available)

    def _fill_sources(self) -> None:
        profile = self.profile or {}
        parts = [f"<h3>{html_escape(profile_name(profile))}</h3>"]
        evidence = profile.get("evidence") or []
        if evidence:
            parts.append(f"<p><b>{html_escape(tr('meta.page.sources'))}</b></p><ul>")
            for entry in evidence:
                key = entry.get("key")
                title = html_escape(tr(key + ".title") if key else entry.get("title", ""))
                detail = html_escape(tr(key + ".detail") if key else entry.get("detail", ""))
                url = entry.get("url")
                head = f'<a href="{html_escape(url)}">{title}</a>' if url else title
                parts.append(f"<li>{head}<br>{detail}</li>")
            parts.append("</ul>")
        limitations = profile.get("limitations") or []
        if limitations:
            parts.append(f"<p><b>{html_escape(tr('meta.page.limitations'))}</b></p><ul>")
            parts += [f"<li>{html_escape(tr(text))}</li>" for text in limitations]
            parts.append("</ul>")
        parts.append(f"<p><b>{html_escape(tr('meta.page.how.title'))}</b></p><ul>")
        parts += [f"<li>{html_escape(tr(f'meta.page.how.{index}'))}</li>" for index in range(1, 6)]
        parts.append("</ul>")
        self.sources_view.setHtml("".join(parts))

    # ---------------------------------------------------------------- manual editing

    def _pick_source_file(self) -> None:
        if not exiftool_available():
            self._set_status("deps.exiftool_missing")
            return
        patterns = photo_filter() if self.media == "photo" else video_filter()
        selected, _filter = QFileDialog.getOpenFileName(self, tr("meta.page.pick_title"), default_dialog_dir(), patterns)
        if not selected:
            return
        path = Path(selected)
        try:
            self.source_meta = read_metadata(path)
        except Exception as exc:
            self._set_status("meta.page.read_failed", error=str(exc))
            return
        self.source_name = path.name
        self.source_label.setText(tr("meta.page.source_loaded", name=path.name, n=len(self.source_meta)))
        self._fill_source_table()

    def _fill_source_table(self) -> None:
        table = self.source_table
        table.setRowCount(0)
        for key, value in self.source_meta.items():
            if key == "SourceFile" or key.startswith(("System:", "ExifTool:")):
                continue
            spec = field_spec(self.media, key)
            row = table.rowCount()
            table.insertRow(row)
            tag = QTableWidgetItem(key)
            text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
            value_item = QTableWidgetItem(text[:300])
            value_item.setToolTip(text[:2000])
            support = QTableWidgetItem(tr("meta.page.src.yes") if spec else tr("meta.page.src.read_only"))
            if spec:
                support.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                support.setCheckState(Qt.Unchecked)
                support.setData(Qt.UserRole, key)
            else:
                support.setForeground(QColor(self.tokens["faint"]))
            table.setItem(row, 0, tag)
            table.setItem(row, 1, value_item)
            table.setItem(row, 2, support)
        self._filter_source_table(self.search.text())

    def _filter_source_table(self, text: str) -> None:
        needle = text.strip().lower()
        for row in range(self.source_table.rowCount()):
            tag = self.source_table.item(row, 0).text().lower()
            value = self.source_table.item(row, 1).text().lower()
            self.source_table.setRowHidden(row, bool(needle) and needle not in tag and needle not in value)

    def _import_checked(self) -> None:
        if self.profile is None or not self.profile.get("available", True):
            return
        imported, rejected = [], []
        numeric = {}
        for row in range(self.source_table.rowCount()):
            support = self.source_table.item(row, 2)
            if support.checkState() != Qt.Checked:
                continue
            key = support.data(Qt.UserRole)
            spec = field_spec(self.media, key)
            if not numeric:
                numeric = self._numeric_source()
            value = numeric.get(key, self.source_table.item(row, 1).text())
            value = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
            error = validate_value(spec, value)
            if error:
                rejected.append(f"{spec.label}: {error}")
                continue
            self.profile.setdefault("fields", {})[key] = value
            imported.append(spec.label)
        if imported:
            self._mark_dirty("meta.page.change.imported", n=len(imported))
            self._fill_fields()
        if rejected:
            self._set_status("meta.page.not_imported", items="; ".join(rejected[:3]))
        elif not imported:
            self._set_status("meta.page.check_fields")

    def _numeric_source(self) -> dict:
        for key in ("SourceFile",):
            source = self.source_meta.get(key)
            if source:
                try:
                    return read_metadata(Path(source), numeric=True)
                except Exception:
                    return {}
        return {}

    # ---------------------------------------------------------------- persistence

    def _reset_builtin(self) -> None:
        if not self.profile or not self.profile.get("builtin"):
            return
        profile = self.store.reset(self.profile["id"])
        self._set_status("meta.page.reset_done")
        self.dirty = False
        self._fill_list(select=profile["id"])

    def _save_as(self) -> None:
        if not self.profile:
            return
        name, ok = QInputDialog.getText(self, tr("meta.page.save_title"), tr("meta.page.save_label"),
                                        text=tr("meta.page.save_default", name=profile_name(self.profile)))
        if not ok or not name.strip():
            return
        new = self.store.save_as(self.profile, name.strip()[:80])
        self.dirty = False
        self._set_status("meta.page.saved", name=new["name"])
        self._fill_list(select=new["id"])

    def _delete_custom(self) -> None:
        if not self.profile or self.profile.get("builtin", True):
            return
        if QMessageBox.question(self, tr("meta.page.delete"), tr("meta.page.delete_confirm", name=profile_name(self.profile))) != QMessageBox.Yes:
            return
        self.store.delete(self.profile["id"])
        self.dirty = False
        self._fill_list(select=self.store.active_id(self.media))

    def _apply(self) -> None:
        if not self.profile or not self.profile.get("available", True):
            return
        invalid = [key for key, value in self.profile.get("fields", {}).items()
                   if field_spec(self.media, key) is None or validate_value(field_spec(self.media, key), value)]
        if invalid:
            self._set_status("meta.page.fix_errors", fields=", ".join(invalid[:4]))
            return
        if self.dirty:
            self.store.save(self.profile)
        self.store.set_active(self.media, self.profile["id"])
        self.applied[self.media] = self.profile["id"]
        self.dirty = False
        self._finish()

    # ---------------------------------------------------------------- page life cycle

    def open_in(self, rect) -> None:
        """Show over the whole window content, sliding up slightly unless motion is reduced."""
        self.setGeometry(rect)
        self.raise_()
        self.show()
        self.setFocus()
        if not prefers_reduced_motion():
            self._slide.setStartValue(float(min(28, rect.height() // 20)))
            self._slide.setEndValue(0.0)
            self._slide.start()

    def set_theme(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens
        for widget in self.themed:
            widget.set_theme(tokens)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.close_page()
            return
        super().keyPressEvent(event)

    def close_page(self) -> None:
        if self.dirty and not self._confirm_discard():
            return
        self._finish()

    def _finish(self) -> None:
        self._slide.stop()
        self.hide()
        self.closed.emit()


class LanguageButton(QAbstractButton):
    """Round header button with the short code of the interface language; opens LanguageMenu."""

    chosen = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(40, 40)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setFocusPolicy(Qt.TabFocus)
        self._font = ui_font(12, QFont.Bold)
        self.tokens = THEMES["light"]
        self.menu: LanguageMenu | None = None
        self._menu_closed_at = 0.0
        self.clicked.connect(self._toggle_menu)
        self.retranslate_ui()

    def set_theme(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens

    def retranslate_ui(self) -> None:
        name = NATIVE_NAMES[current_locale()]
        self.setToolTip(tr("language.tooltip", language=name))
        self.setAccessibleName(tr("language.accessible"))
        self.setAccessibleDescription(name)
        self.update()

    def _toggle_menu(self) -> None:
        if time.monotonic() - self._menu_closed_at < 0.25:
            return
        self.menu = LanguageMenu(current_locale(), self.tokens, self)
        self.menu.selected.connect(self.chosen.emit)
        self.menu.closed.connect(lambda: setattr(self, "_menu_closed_at", time.monotonic()))
        position = self.mapToGlobal(QPoint(self.width() - self.menu.width(), self.height() + 8))
        screen = QApplication.screenAt(position) or self.screen()
        if screen:
            available = screen.availableGeometry()
            position.setX(max(available.left() + 12, min(position.x(), available.right() - self.menu.width() - 12)))
        self.menu.move(position)
        self.menu.show()
        self.menu.setFocus()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space, Qt.Key_Down):
            self._toggle_menu()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(BRAND_LIME), 1.5) if self.hasFocus() else Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 70 if self.isDown() else 52 if self.underMouse() else 34))
        painter.drawEllipse(rect)
        text = SHORT_NAMES[current_locale()]
        font = QFont(self._font)
        metrics = QFontMetricsF(font)
        if metrics.horizontalAdvance(text) > rect.width() - 10:
            font.setPixelSize(10)
        painter.setFont(font)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(rect, Qt.AlignCenter, text)


class LanguageMenu(QWidget):
    """Popup list of the supported languages, each in its own script."""

    selected = Signal(str)
    closed = Signal()

    ROW = 36
    PAD = 8

    def __init__(self, current: str, tokens: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.locales = list(SUPPORTED_LOCALES)
        self.current = current
        self.hover_index = self.locales.index(current) if current in self.locales else 0
        self.tokens = tokens
        self._font = ui_font(13, QFont.DemiBold)
        self._code_font = ui_font(11, QFont.Bold)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setAccessibleName(tr("language.accessible"))
        width = max(QFontMetricsF(self._font).horizontalAdvance(NATIVE_NAMES[code]) for code in self.locales)
        self.setFixedSize(int(width) + 100, self.PAD * 2 + self.ROW * len(self.locales))

    def _row_at(self, y: float) -> int | None:
        index = int((y - self.PAD) // self.ROW)
        return index if 0 <= index < len(self.locales) and y >= self.PAD else None

    def mouseMoveEvent(self, event) -> None:
        index = self._row_at(event.position().y())
        if index is not None and index != self.hover_index:
            self.hover_index = index
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        index = self._row_at(event.position().y())
        if index is not None and self.rect().contains(event.position().toPoint()):
            self.close()
            self.selected.emit(self.locales[index])

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Down:
            self.hover_index = min(len(self.locales) - 1, self.hover_index + 1)
            self.update()
        elif event.key() == Qt.Key_Up:
            self.hover_index = max(0, self.hover_index - 1)
            self.update()
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.close()
            self.selected.emit(self.locales[self.hover_index])
        else:
            super().keyPressEvent(event)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.closed.emit()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        tokens = self.tokens
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(tokens["menu_border"]), 1))
        painter.setBrush(QColor(tokens["menu_bg"]))
        painter.drawRoundedRect(rect, 20, 20)
        metrics = QFontMetricsF(self._font)
        code_metrics = QFontMetricsF(self._code_font)
        for index, code in enumerate(self.locales):
            row = QRectF(6, self.PAD + index * self.ROW, rect.width() - 12, self.ROW)
            selected = code == self.current
            if index == self.hover_index:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(tokens["menu_hover"]))
                painter.drawRoundedRect(row, 14, 14)
            center_y = row.center().y()
            if selected:
                painter.setPen(QPen(QColor(tokens["text"]), 1.2))
                painter.setBrush(QColor(BRAND_LIME))
                painter.drawEllipse(QPointF(row.left() + 16, center_y), 4, 4)
            painter.setFont(self._font)
            painter.setPen(QColor(tokens["accent_text"] if selected else tokens["text"]))
            painter.drawText(QPointF(row.left() + 32, center_y + metrics.capHeight() / 2), NATIVE_NAMES[code])
            painter.setFont(self._code_font)
            painter.setPen(QColor(tokens["muted"]))
            short = SHORT_NAMES[code]
            painter.drawText(
                QPointF(row.right() - 14 - code_metrics.horizontalAdvance(short), center_y + code_metrics.capHeight() / 2), short
            )


class TaskRunner(QObject):
    """Runs a blocking function on a worker thread; results come back as queued Qt signals."""

    progress = Signal(object)
    finished = Signal(object)
    failed = Signal(object)

    def start(self, function: Callable, *args) -> None:
        def target() -> None:
            try:
                result = function(*args, self.progress.emit)
            except BaseException as exc:  # reported to the UI, never swallowed silently
                self.failed.emit(exc)
            else:
                self.finished.emit(result)

        threading.Thread(target=target, daemon=True).start()


TOOL_TITLES = {"ffmpeg": "FFmpeg", "exiftool": "ExifTool"}


class DependencyController(QObject):
    """State and operations of the component manager (FFmpeg/FFprobe, ExifTool).

    Nothing is bundled: tools are found on the system, chosen manually, or downloaded on
    explicit request from pinned upstream builds with SHA-256 verification.
    """

    changed = Signal()

    def __init__(self, settings: AppSettings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.statuses: dict[str, dependencies.ToolStatus] = {}
        self.detecting = False
        self.detected_once = False
        self.busy = ""  # "" | "install" | "updates"
        self.progress: tuple[str, str, int, int] | None = None  # tool, stage, done, total
        self.message: tuple[str, dict, bool] | None = None  # key, params, is_error
        self.updates: dict[str, dependencies.Release] = {}
        self.cancel_event = threading.Event()
        self._runners: list[TaskRunner] = []

    # ------------------------------------------------------------ queries

    def manual_path(self, tool: str) -> str:
        return self.settings.get(f"tool_path.{tool}") or ""

    def status(self, tool: str) -> dependencies.ToolStatus:
        return self.statuses.get(tool) or dependencies.ToolStatus(tool, detail="deps.detail.not_found")

    def ok(self, tool: str) -> bool:
        return self.status(tool).ok

    def missing(self) -> list[str]:
        return [tool for tool in dependencies.TOOLS if not self.ok(tool)]

    def supported_platform(self) -> bool:
        return dependencies.pinned_release("ffmpeg") is not None

    # ------------------------------------------------------------ operations

    def _runner(self) -> TaskRunner:
        runner = TaskRunner(self)
        self._runners.append(runner)
        runner.finished.connect(lambda _r, item=runner: self._runners.remove(item) if item in self._runners else None)
        runner.failed.connect(lambda _e, item=runner: self._runners.remove(item) if item in self._runners else None)
        return runner

    def detect_all(self) -> None:
        if self.detecting:
            return
        self.detecting = True
        self.changed.emit()
        manual = {tool: self.manual_path(tool) for tool in dependencies.TOOLS}

        def work(_progress):
            return {tool: dependencies.detect(tool, manual.get(tool)) for tool in dependencies.TOOLS}

        runner = self._runner()
        runner.finished.connect(self._detected)
        runner.failed.connect(lambda exc: self._detected({}))
        runner.start(work)

    def _detected(self, statuses: dict) -> None:
        self.statuses.update(statuses)
        self.detecting = False
        self.detected_once = True
        self._apply_paths()
        self.changed.emit()

    def _apply_paths(self) -> None:
        ffmpeg = self.status("ffmpeg")
        exiftool = self.status("exiftool")
        _TOOL_PATHS["ffmpeg"] = ffmpeg.path if ffmpeg.ok else ""
        _TOOL_PATHS["exiftool"] = exiftool.path if exiftool.ok else ""
        set_exiftool_path(exiftool.path if exiftool.ok else None)

    def set_manual_path(self, tool: str, path: str | None) -> None:
        self.settings.set(f"tool_path.{tool}", path or None)
        self.message = None
        self.detect_all()

    def install(self, tools: list[str] | None = None, updates: bool = False) -> None:
        if self.busy or self.detecting:
            return
        releases = []
        for tool in tools or self.missing():
            release = self.updates.get(tool) if updates else dependencies.pinned_release(tool)
            if release is not None:
                releases.append(release)
        if not releases:
            if not self.supported_platform():
                self.message = ("deps.msg.unsupported_platform", {"platform": dependencies.platform_key()}, True)
                self.changed.emit()
            return
        self.busy = "install"
        self.cancel_event.clear()
        self.message = None
        self.progress = (releases[0].tool, "download", 0, 0)
        self.changed.emit()

        def work(progress):
            installed = {}
            for release in releases:
                def report(stage, done, total, tool=release.tool):
                    progress((tool, stage, done, total))

                installed[release.tool] = dependencies.install(release, progress=report, cancel=self.cancel_event)
            return installed

        runner = self._runner()
        runner.progress.connect(self._on_progress)
        runner.finished.connect(self._installed)
        runner.failed.connect(self._install_failed)
        runner.start(work)

    def _on_progress(self, payload) -> None:
        self.progress = payload
        self.changed.emit()

    def _installed(self, installed: dict) -> None:
        self.busy = ""
        self.progress = None
        for tool in installed:
            self.updates.pop(tool, None)
            # A manual choice would hide the fresh managed copy; the user asked for this install.
            if self.manual_path(tool):
                self.settings.set(f"tool_path.{tool}", None)
        names = ", ".join(TOOL_TITLES[tool] + " " + status.version.split("-")[0] for tool, status in installed.items())
        self.message = ("deps.msg.installed", {"names": names}, False)
        self.detect_all()

    def _install_failed(self, exc: BaseException) -> None:
        self.busy = ""
        self.progress = None
        if isinstance(exc, dependencies.DownloadError):
            params = dict(exc.params)
            if "detail" in params:
                params["detail"] = tr(params["detail"])
            self.message = (exc.key, params, exc.key != "deps.error.cancelled")
        else:
            self.message = ("deps.error.unexpected", {"error": str(exc)[:200]}, True)
        self.detect_all()

    def cancel(self) -> None:
        self.cancel_event.set()

    def check_updates(self) -> None:
        if self.busy or self.detecting:
            return
        self.busy = "updates"
        self.message = None
        self.changed.emit()

        def work(_progress):
            found, errors = {}, []
            for tool in dependencies.TOOLS:
                try:
                    release = dependencies.latest_release(tool)
                except Exception as exc:
                    errors.append(f"{TOOL_TITLES[tool]}: {str(exc)[:120]}")
                    continue
                if release is not None:
                    found[tool] = release
            return found, errors

        runner = self._runner()
        runner.finished.connect(self._updates_checked)
        runner.failed.connect(lambda exc: self._updates_checked(({}, [str(exc)[:160]])))
        runner.start(work)

    def _updates_checked(self, result) -> None:
        found, errors = result
        self.busy = ""
        self.updates = {}
        for tool, release in found.items():
            status = self.status(tool)
            current = status.detail_params.get("installed") or status.version
            if not status.ok or dependencies.is_newer(release.version, current):
                self.updates[tool] = release
        if errors and not found:
            self.message = ("deps.msg.update_check_failed", {"error": "; ".join(errors)}, True)
        elif self.updates:
            names = ", ".join(f"{TOOL_TITLES[t]} {r.version}" for t, r in self.updates.items())
            self.message = ("deps.msg.updates_available", {"names": names}, False)
        else:
            self.message = ("deps.msg.up_to_date", {}, False)
        self.changed.emit()


class ToolRow(QFrame):
    """One component in Settings: status, version, source, path and its own actions."""

    def __init__(self, tool: str, page: "SettingsPage") -> None:
        super().__init__()
        self.tool = tool
        self.page = page
        self.setObjectName("ToolRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 12, 12)
        layout.setSpacing(12)
        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        layout.addWidget(self.dot, 0, Qt.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.title = QLabel(TOOL_TITLES[tool])
        self.title.setObjectName("ToolTitle")
        self.title.setFont(ui_font(14, QFont.Bold))
        head.addWidget(self.title)
        self.state = QLabel()
        self.state.setObjectName("DialogMuted")
        self.state.setFont(ui_font(12, QFont.DemiBold))
        head.addWidget(self.state)
        head.addStretch(1)
        text.addLayout(head)
        self.purpose = QLabel()
        self.purpose.setObjectName("DialogMuted")
        self.purpose.setFont(ui_font(12, QFont.Medium))
        self.purpose.setWordWrap(True)
        text.addWidget(self.purpose)
        self.detail = QLabel()
        self.detail.setObjectName("DialogMuted")
        self.detail.setFont(ui_font(12, QFont.Medium))
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text.addWidget(self.detail)
        layout.addLayout(text, 1)
        self.choose = page._t(PillButton("", "outline"))
        self.choose.clicked.connect(self._choose_path)
        layout.addWidget(self.choose, 0, Qt.AlignVCenter)
        self.clear = page._t(PillButton("", "outline"))
        self.clear.clicked.connect(lambda: page.controller.set_manual_path(tool, None))
        layout.addWidget(self.clear, 0, Qt.AlignVCenter)

    def _choose_path(self) -> None:
        title = tr("settings.choose_title", tool=TOOL_TITLES[self.tool])
        start = str(Path(self.page.controller.status(self.tool).path).parent) if self.page.controller.status(self.tool).path else default_dialog_dir()
        selected, _filter = QFileDialog.getOpenFileName(self, title, start)
        if selected:
            self.page.controller.set_manual_path(self.tool, selected)

    def render(self) -> None:
        controller = self.page.controller
        status = controller.status(self.tool)
        tokens = self.page.tokens
        if controller.detecting and not controller.detected_once:
            color, state = tokens["faint"], tr("deps.state.checking")
        else:
            color = {"ok": BRAND_LIME, "incompatible": "#FFB020", "broken": BRAND_ERROR}.get(status.state, BRAND_ERROR)
            state = tr(f"deps.state.{status.state}")
        self.dot.setStyleSheet(f"background: {color}; border-radius: 5px; border: 1px solid {tokens['field_border']};")
        self.state.setText(state)
        self.purpose.setText(tr(f"deps.purpose.{self.tool}"))
        lines = []
        if status.ok:
            lines.append(tr("deps.line.version", version=status.version))
            lines.append(tr(f"deps.source.{status.source or 'system'}"))
            lines.append(tr("deps.line.path", path=compact_path(Path(status.path), 64)))
        else:
            params = {key: (tr(value) if isinstance(value, str) and value.startswith("deps.") else value)
                      for key, value in status.detail_params.items()}
            if status.detail:
                lines.append(tr(status.detail, **params))
            if status.path:
                lines.append(tr("deps.line.path", path=compact_path(Path(status.path), 64)))
        update = controller.updates.get(self.tool)
        if update is not None:
            lines.append(tr("deps.line.update", version=update.version))
        self.detail.setText("\n".join(lines))
        self.detail.setToolTip(status.path or "")
        busy = bool(controller.busy) or controller.detecting
        self.choose.setText(tr("settings.choose_path"))
        self.choose.setToolTip(tr("settings.choose_tooltip", tool=TOOL_TITLES[self.tool]))
        self.choose.setEnabled(not busy)
        self.clear.setText(tr("settings.clear_path"))
        self.clear.setToolTip(tr("settings.clear_tooltip"))
        self.clear.setVisible(bool(controller.manual_path(self.tool)))
        self.clear.setEnabled(not busy)
        for button in (self.choose, self.clear):
            button.updateGeometry()


class SettingsPage(QFrame):
    """Settings shown inside the main window: components (dependency manager) and About."""

    closed = Signal()

    def __init__(self, controller: DependencyController, tokens: dict[str, str], parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("SettingsPage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.controller = controller
        self.tokens = tokens
        self.themed: list[QWidget] = []
        self._texts: list[tuple[Callable[[str], None], str]] = []

        self._slide = QVariantAnimation(self)
        self._slide.setDuration(220)
        self._slide.setEasingCurve(QEasingCurve.OutCubic)
        self._slide.valueChanged.connect(lambda offset: self.move(0, int(offset)))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("SettingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("SettingsContent")
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        top = QHBoxLayout()
        title = self._t(DisplayLabel("", 20))
        self._tx(lambda text: title.set_text(text.upper()), "settings.title")
        top.addWidget(title, 0, Qt.AlignVCenter)
        top.addStretch(1)
        close = self._t(PillButton("", "outline"))
        self._tx(lambda text: (close.setText(text), close.updateGeometry()), "common.close")
        self._tx(close.setToolTip, "settings.close_tooltip")
        close.clicked.connect(self.close_page)
        top.addWidget(close)
        root.addLayout(top)

        # ---- components
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(10)
        heading = self._t(DisplayLabel("", 14, width_axis=128))
        self._tx(lambda text: heading.set_text(text.upper()), "settings.components")
        layout.addWidget(heading)
        self.platform_label = self._muted("")
        layout.addWidget(self.platform_label)
        intro = self._muted("")
        intro.setWordWrap(True)
        self._tx(intro.setText, "settings.components_intro")
        layout.addWidget(intro)
        self.rows = {tool: ToolRow(tool, self) for tool in dependencies.TOOLS}
        for row in self.rows.values():
            layout.addWidget(row)

        self.progress = ProgressLine()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.message = QLabel()
        self.message.setObjectName("DialogMuted")
        self.message.setFont(ui_font(12, QFont.DemiBold))
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.message)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.install_button = self._t(PillButton(""))
        self.install_button.clicked.connect(self._install_clicked)
        actions.addWidget(self.install_button)
        self.updates_button = self._t(PillButton("", "outline"))
        self.updates_button.clicked.connect(self._updates_clicked)
        actions.addWidget(self.updates_button)
        self.recheck_button = self._t(PillButton("", "outline"))
        self.recheck_button.clicked.connect(self.controller.detect_all)
        actions.addWidget(self.recheck_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        notes = self._muted("")
        notes.setWordWrap(True)
        self._tx(notes.setText, "settings.components_notes")
        layout.addWidget(notes)
        root.addWidget(card)

        # ---- about
        about = QFrame()
        about.setObjectName("Card")
        about_layout = QVBoxLayout(about)
        about_layout.setContentsMargins(22, 18, 22, 18)
        about_layout.setSpacing(8)
        about_heading = self._t(DisplayLabel("", 14, width_axis=128))
        self._tx(lambda text: about_heading.set_text(text.upper()), "settings.about")
        about_layout.addWidget(about_heading)
        self.about_text = QTextBrowser()
        self.about_text.setObjectName("AboutText")
        self.about_text.setOpenLinks(False)
        self.about_text.anchorClicked.connect(lambda url: open_url(url.toString()))
        self.about_text.setFrameShape(QFrame.NoFrame)
        self.about_text.setMinimumHeight(170)
        self.about_text.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        about_layout.addWidget(self.about_text)
        root.addWidget(about)
        root.addStretch(1)

        for widget in self.themed:
            widget.set_theme(tokens)
        controller.changed.connect(self.render)
        self.render()

    # ------------------------------------------------------------ helpers

    def _t(self, widget: QWidget) -> QWidget:
        self.themed.append(widget)
        return widget

    def _tx(self, setter: Callable[[str], None], key: str) -> None:
        self._texts.append((setter, key))
        setter(tr(key))

    def _muted(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("DialogMuted")
        label.setFont(ui_font(12, QFont.Medium))
        return label

    # ------------------------------------------------------------ rendering

    def retranslate_ui(self) -> None:
        for setter, key in self._texts:
            setter(tr(key))
        self.render()

    def render(self) -> None:
        controller = self.controller
        from uniqlab.platform_support import platform_label

        self.platform_label.setText(tr("settings.platform", platform=platform_label()))
        for row in self.rows.values():
            row.render()
        busy = controller.busy
        missing = controller.missing()
        if busy == "install":
            self.install_button.setText(tr("settings.cancel"))
            self.install_button.setEnabled(True)
        elif controller.updates:
            self.install_button.setText(tr("settings.install_updates", n=len(controller.updates)))
            self.install_button.setEnabled(not busy and not controller.detecting)
        elif missing:
            self.install_button.setText(tr("settings.install_all"))
            self.install_button.setEnabled(not busy and not controller.detecting and controller.supported_platform())
        else:
            self.install_button.setText(tr("settings.all_installed"))
            self.install_button.setEnabled(False)
        self.updates_button.setText(tr("settings.checking_updates") if busy == "updates" else tr("settings.check_updates"))
        self.updates_button.setEnabled(not busy and not controller.detecting)
        self.updates_button.setToolTip(tr("settings.check_updates_tooltip"))
        self.recheck_button.setText(tr("settings.recheck"))
        self.recheck_button.setEnabled(not busy and not controller.detecting)
        for button in (self.install_button, self.updates_button, self.recheck_button):
            button.updateGeometry()

        progress = controller.progress
        self.progress.setVisible(progress is not None)
        if progress is not None:
            tool, stage, done, total = progress
            percent = int(done / total * 100) if total else 0
            self.progress.setValue(percent if stage == "download" else 100)
            if stage == "download" and total:
                text = tr("deps.progress.download_bytes", tool=TOOL_TITLES[tool], done=format_size(done),
                          total=format_size(total), percent=percent)
            elif stage == "download":
                text = tr("deps.progress.download", tool=TOOL_TITLES[tool], done=format_size(done))
            else:
                text = tr(f"deps.progress.{stage}", tool=TOOL_TITLES[tool])
            self._set_message(text, False)
        elif busy == "updates":
            self._set_message(tr("settings.checking_updates"), False)
        elif controller.message:
            key, params, is_error = controller.message
            self._set_message(tr(key, **params), is_error)
        elif controller.detected_once and missing:
            self._set_message(tr("settings.missing_hint", names=", ".join(TOOL_TITLES[t] for t in missing)), False)
        else:
            self._set_message("", False)
        self._render_about()

    def _set_message(self, text: str, is_error: bool) -> None:
        self.message.setText(text)
        color = BRAND_ERROR if is_error else self.tokens["muted"]
        self.message.setStyleSheet(f"color: {color};")
        self.message.setVisible(bool(text))

    def _render_about(self) -> None:
        link = self.tokens["accent_text"]
        text = self.tokens["text"]
        muted = self.tokens["muted"]
        html = (
            f"<div style='color:{text}'>"
            f"<p><b>{html_escape(APP_NAME)}</b> {html_escape(__version__)} · VIDEO &amp; IMAGE PROCESSING<br>"
            f"<a style='color:{link}' href='{AUTHOR_URL}'>by {html_escape(AUTHOR)}</a></p>"
            f"<p style='color:{muted}'>{html_escape(tr('about.license'))}</p>"
            f"<p style='color:{muted}'>{html_escape(tr('about.privacy'))}</p>"
            f"<p style='color:{muted}'>{html_escape(tr('about.third_party'))}</p>"
            "</div>"
        )
        self.about_text.setHtml(html)

    # ------------------------------------------------------------ actions

    def _install_clicked(self) -> None:
        controller = self.controller
        if controller.busy == "install":
            controller.cancel()
        elif controller.updates:
            controller.install(list(controller.updates), updates=True)
        else:
            controller.install()

    def _updates_clicked(self) -> None:
        self.controller.check_updates()

    # ------------------------------------------------------------ life cycle

    def open_in(self, rect) -> None:
        self.setGeometry(rect)
        self.raise_()
        self.show()
        self.setFocus()
        if not prefers_reduced_motion():
            self._slide.setStartValue(float(min(28, rect.height() // 20)))
            self._slide.setEndValue(0.0)
            self._slide.start()

    def set_theme(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens
        for widget in self.themed:
            widget.set_theme(tokens)
        self.render()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.close_page()
            return
        super().keyPressEvent(event)

    def close_page(self) -> None:
        self._slide.stop()
        self.hide()
        self.closed.emit()


class UniqLabWindow(QMainWindow):
    def __init__(self, settings: AppSettings | None = None) -> None:
        super().__init__()
        self.settings = settings or AppSettings()

        self.selected_videos: list[Path] = []
        self.selected_size = 0
        # How the current selection was made, as an i18n key + params (re-rendered on language change).
        self.selection_origin: tuple[str, dict] = ("", {})
        # Video and Photo keep their own selections; switching modes swaps them.
        self.media_mode = "video"
        self.mode_state: dict[str, dict] = {
            mode: {"files": [], "size": 0, "origin": ("", {}), "last_dir": None} for mode in ("video", "photo")
        }
        self.photo_intensity = "medium"
        self.metadata_page: MetadataPage | None = None
        self.settings_page: SettingsPage | None = None
        self.profile_store = ProfileStore(app_data_dir() / "metadata_profiles.json")
        self.controller = DependencyController(self.settings, self)
        self.controller.changed.connect(self._tools_changed)
        self.last_input_dir: Path | None = None
        self.output_dir: Path | None = None
        self.worker: threading.Thread | None = None
        self.log_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.start_time = 0.0
        self.aspect_name = ASPECT_OPTIONS[0][0]
        self.stop_event = threading.Event()
        self.current_process: subprocess.Popen | None = None
        self.process_lock = threading.Lock()
        saved_theme = self.settings.get("theme")
        self.theme_mode = saved_theme if saved_theme in ("light", "dark", "system") else "system"
        self.dark_theme = self._system_prefers_dark() if self.theme_mode == "system" else self.theme_mode == "dark"
        self.themed_widgets: list[QWidget] = []
        self._texts: list[tuple[Callable[[str], None], str]] = []
        self._offer_pending = True

        # Processing panel state: idle -> running -> (stopping) -> finished.
        self.run_state = "idle"
        self.run_total = 0
        self.run_processed = 0
        self.run_attempted = 0
        self.run_elapsed = 0.0
        self.run_errors: list[str] = []
        self.run_warnings: list[str] = []
        self.run_stopped = False
        self.run_output_dir: Path | None = None
        self.current_file = ""
        self.notice: tuple[str, dict] | None = None

        self.setWindowTitle(APP_NAME)
        icon_path = resource_path("assets/app_icon.png")
        if should_set_runtime_icon() and icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1160, 820)
        self.setMinimumSize(980, 780)

        self._build_ui()
        self.theme_reveal = ThemeReveal(self)
        self.theme_reveal.keep_live = [self.mode_switch, self.theme_button, self.language_button]
        self.mode_switch.set_mode(self.media_mode, animate=False)
        self.theme_button.set_mode(self.theme_mode)
        self._sync_mode_ui()
        self._update_aspect_from_combo()
        self._apply_styles()
        self._connect_system_theme_listener()
        self._tools_changed()
        self._refresh_panel()
        add_listener(lambda _locale: self.retranslate_ui())

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._drain_log_queue)
        self.timer.start(120)

        self.clock = QTimer(self)
        self.clock.timeout.connect(self._tick)
        self.clock.start(500)

        QTimer.singleShot(150, self.controller.detect_all)

    def _themed(self, widget: QWidget) -> QWidget:
        self.themed_widgets.append(widget)
        return widget

    def _tx(self, setter: Callable[[str], None], key: str) -> None:
        self._texts.append((setter, key))
        setter(tr(key))

    # ------------------------------------------------------------------ layout

    def _build_ui(self) -> None:
        scroll_area = QScrollArea()
        scroll_area.setObjectName("MainScroll")
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setCentralWidget(scroll_area)

        root = QWidget()
        root.setObjectName("Root")
        root.setMinimumWidth(960)
        scroll_area.setWidget(root)

        main = QVBoxLayout(root)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        sheet = QFrame()
        sheet.setObjectName("Sheet")
        sheet_layout = QVBoxLayout(sheet)
        sheet_layout.setContentsMargins(24, 20, 24, 20)
        sheet_layout.setSpacing(14)
        main.addWidget(sheet, 1)

        # "Blue Island": the header is an inset cobalt panel aligned with the workspace below.
        island = CobaltPanel()
        island.setObjectName("HeaderIsland")
        header = QHBoxLayout(island)
        header.setContentsMargins(20, 14, 16, 14)
        header.setSpacing(10)
        self.wordmark = BrandWordmark()
        header.addWidget(self.wordmark, 0, Qt.AlignVCenter)
        header.addStretch(1)
        # Media mode is the main choice here; theme and language live in small round buttons next to it.
        self.mode_switch = ThemeSwitch(options=(("video", "mode.video"), ("photo", "mode.photo")))
        for mode, button in self.mode_switch.buttons.items():
            button.setMinimumWidth(86)
            button.clicked.connect(lambda _checked=False, selected=mode: self.set_media_mode(selected))
        header.addWidget(self.mode_switch, 0, Qt.AlignVCenter)
        header.addSpacing(2)
        self.theme_button = ThemeButton()
        self.theme_button.clicked.connect(lambda: self.set_theme_mode(self.theme_button.next_mode()))
        header.addWidget(self.theme_button, 0, Qt.AlignVCenter)
        self.language_button = self._themed(LanguageButton())
        self.language_button.chosen.connect(self.set_language)
        header.addWidget(self.language_button, 0, Qt.AlignVCenter)
        header.addSpacing(4)
        header.addWidget(HeaderDivider(), 0, Qt.AlignVCenter)
        header.addSpacing(4)
        self.status_pill = StatusPill(interactive=True)
        self.status_pill.clicked.connect(self.open_settings_page)
        header.addWidget(self.status_pill, 0, Qt.AlignVCenter)
        sheet_layout.addWidget(island)
        sheet_layout.addSpacing(4)

        workspace = QHBoxLayout()
        workspace.setSpacing(18)
        workspace.addWidget(self._prep_card(), 62)
        workspace.addWidget(self._process_panel(), 38)
        sheet_layout.addLayout(workspace, 1)

        self.log_box = QPlainTextEdit()
        self.log_box.setObjectName("LogBox")
        self.log_box.setReadOnly(True)
        self.log_box.setFont(mono_font(12))
        self._tx(self.log_box.setPlaceholderText, "logs.placeholder")
        self.log_panel = self._themed(LogPanel(self.log_box))
        self.log_panel.toggled.connect(lambda _expanded: self._fit_log_height())
        sheet_layout.addWidget(self.log_panel)

    def _section(self, number: str, title_key: str | None, content: QLayout, align_top: bool = False) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(16)
        label = QWidget()
        label.setFixedWidth(132)
        label_layout = QVBoxLayout(label)
        label_layout.setContentsMargins(0, 2 if align_top else 0, 0, 0)
        label_layout.setSpacing(4)
        label_layout.addWidget(self._themed(DisplayLabel(number, 11, "accent_text")))
        title = self._themed(DisplayLabel("", 14, width_axis=128))
        if title_key:
            self._tx(lambda text, widget=title: widget.set_text(self._fit_title(text.upper())), title_key)
        self.last_section_title = title
        label_layout.addWidget(title)
        row.addWidget(label, 0, Qt.AlignTop if align_top else Qt.AlignVCenter)
        row.addLayout(content, 1)
        return row

    @staticmethod
    def _fit_title(text: str) -> str:
        # Section titles sit in a 132 px column; long translations wrap onto a second line.
        metrics = QFontMetricsF(display_font(14, 128))
        if metrics.horizontalAdvance(text) <= 130 or " " not in text:
            return text
        words = text.split(" ")
        first = words[0]
        for index in range(1, len(words)):
            candidate = " ".join(words[: index + 1])
            if metrics.horizontalAdvance(candidate) > 130:
                return " ".join(words[:index]) + "\n" + " ".join(words[index:])
            first = candidate
        return first

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setObjectName("Divider")
        line.setFixedHeight(1)
        return line

    def _prep_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(18)

        videos = QVBoxLayout()
        videos.setSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(8)
        summary = QVBoxLayout()
        summary.setSpacing(2)
        self.video_count = self._themed(DisplayLabel("", 15))
        summary.addWidget(self.video_count)
        self.video_meta = self._themed(ElidedLabel(ui_font(12, QFont.Medium)))
        summary.addWidget(self.video_meta)
        top.addLayout(summary, 1)
        self.source_folder_button = self._themed(PillButton("", "outline"))
        self._tx(lambda text: (self.source_folder_button.setText(text), self.source_folder_button.updateGeometry()),
                 "files.folder")
        self.source_folder_button.clicked.connect(self.choose_source_dir)
        top.addWidget(self.source_folder_button, 0, Qt.AlignVCenter)
        self.source_button = self._themed(PillButton(""))
        self.source_button.clicked.connect(self.choose_source_files)
        top.addWidget(self.source_button, 0, Qt.AlignVCenter)
        videos.addLayout(self._section("01", None, top, align_top=True))
        self.files_title = self.last_section_title
        # The list spans the full card width under the header row.
        self.file_list = self._themed(FileList())
        self.file_list.browseRequested.connect(self.choose_source_files)
        videos.addWidget(self.file_list, 1)
        layout.addLayout(videos, 1)

        layout.addWidget(self._divider())

        export = QHBoxLayout()
        export.setSpacing(8)
        self.output_path_label = self._themed(PathField())
        export.addWidget(self.output_path_label, 1)
        self.output_button = self._themed(PillButton("", "outline"))
        self.output_button.clicked.connect(self.choose_output_dir)
        export.addWidget(self.output_button)
        layout.addLayout(self._section("02", "section.export", export))

        layout.addWidget(self._divider())

        fmt = QHBoxLayout()
        self.option_pages = QStackedWidget()
        self.aspect_combo = self._themed(FormatSelector(ASPECT_OPTIONS))
        self.aspect_combo.currentIndexChanged.connect(self._update_aspect_from_combo)
        self.option_pages.addWidget(self.aspect_combo)
        self.intensity_selector = self._themed(ChipSelector(
            tuple((key, f"photo.intensity.{key}") for key in INTENSITY_KEYS),
            {key: f"photo.intensity_caption.{key}" for key in INTENSITY_KEYS},
        ))
        self.intensity_selector.set_current(self.photo_intensity, emit=False)
        self.intensity_selector.currentChanged.connect(self._update_intensity)
        self.option_pages.addWidget(self.intensity_selector)
        self.option_pages.setFixedHeight(40)
        fmt.addWidget(self.option_pages, 1)
        layout.addLayout(self._section("03", None, fmt))
        self.options_title = self.last_section_title

        layout.addWidget(self._divider())

        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.profile_field = self._themed(ProfileField())
        meta.addWidget(self.profile_field, 1)
        self.metadata_button = self._themed(PillButton("", "outline"))
        self._tx(lambda text: (self.metadata_button.setText(text), self.metadata_button.updateGeometry()), "meta.configure")
        self._tx(self.metadata_button.setToolTip, "meta.configure_tooltip")
        self.metadata_button.clicked.connect(self.open_metadata_dialog)
        meta.addWidget(self.metadata_button)
        layout.addLayout(self._section("04", "section.metadata", meta))
        return card

    def _process_panel(self) -> QFrame:
        panel = CobaltPanel()
        panel.setMinimumWidth(320)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(26, 22, 26, 24)
        layout.setSpacing(0)

        top = QHBoxLayout()
        panel_title = DisplayLabel("", 13, fixed_color="#FFFFFF")
        self._tx(lambda text: panel_title.set_text(text.upper()), "panel.title")
        top.addWidget(panel_title, 0, Qt.AlignVCenter)
        top.addStretch(1)
        self.state_chip = StatusPill(30, 12)
        top.addWidget(self.state_chip, 0, Qt.AlignVCenter)
        layout.addLayout(top)
        layout.addSpacing(20)

        self.metric_label = MetricLabel(58)
        layout.addWidget(self.metric_label)
        layout.addSpacing(8)
        self.metric_caption = ElidedLabel(ui_font(14, QFont.DemiBold), fixed_color=QColor(255, 255, 255, 230))
        layout.addWidget(self.metric_caption)
        layout.addSpacing(16)

        self.progress_bar = ProgressLine()
        layout.addWidget(self.progress_bar)
        layout.addSpacing(14)

        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.stat_done = StatTile("panel.stat.done")
        self.stat_time = StatTile("panel.stat.time")
        self.stat_errors = StatTile("panel.stat.errors")
        for tile in (self.stat_done, self.stat_time, self.stat_errors):
            stats.addWidget(tile)
        layout.addLayout(stats)
        layout.addSpacing(12)

        self.notice_box = NoticeBox()
        layout.addWidget(self.notice_box)
        layout.addStretch(1)
        self.recap = RecapView()
        layout.addWidget(self.recap)
        layout.addSpacing(16)

        self.start_button = ActionButton()
        self.start_button.clicked.connect(self._primary_action)
        layout.addWidget(self.start_button)
        layout.addSpacing(10)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.hint_label = ElidedLabel(ui_font(12, QFont.Medium), fixed_color=QColor(255, 255, 255, 185))
        footer.addWidget(self.hint_label, 1)
        self.open_folder_button = PillButton("", "ghost")
        self._tx(lambda text: (self.open_folder_button.setText(text), self.open_folder_button.updateGeometry()),
                 "panel.open_folder")
        self.open_folder_button.clicked.connect(self.open_output_dir)
        self.open_folder_button.hide()
        footer.addWidget(self.open_folder_button)
        layout.addLayout(footer)
        return panel

    def _fit_log_height(self) -> None:
        if not self.log_panel.is_expanded():
            return
        # Give the expanded log what the workspace can spare, within comfortable bounds.
        root = self.centralWidget().widget()
        spare = self.centralWidget().viewport().height() - (root.minimumSizeHint().height() - self.log_box.height())
        self.log_box.setFixedHeight(max(100, min(230, spare)))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.theme_reveal.stop()
        for page in (self.metadata_page, self.settings_page):
            if page is not None:
                page.setGeometry(self.rect())
        self._fit_log_height()

    def closeEvent(self, event) -> None:
        # Never leave FFmpeg or a download running after the window is gone.
        self.stop_event.set()
        self.controller.cancel()
        with self.process_lock:
            process = self.current_process
        if process is not None:
            terminate_process(process)
        super().closeEvent(event)

    # ------------------------------------------------------------------ styles

    def _apply_styles(self) -> None:
        tokens = THEMES["dark" if self.dark_theme else "light"]
        self.setStyleSheet(
            f"""
            QMainWindow, QScrollArea#MainScroll, QScrollArea#MainScroll > QWidget, QWidget#Root {{
                background: {tokens["sheet"]};
                border: none;
            }}
            QFrame#Sheet {{
                background: {tokens["sheet"]};
                border: none;
            }}
            QFrame#Card {{
                background: {tokens["card"]};
                border: none;
                border-radius: 28px;
            }}
            QFrame#Divider {{
                background: {tokens["field_border"]};
                border: none;
            }}
            QFrame#LogPanel {{
                background: {tokens["card"]};
                border: none;
                border-radius: 22px;
            }}
            QListWidget#FileList {{
                background: {tokens["field"]};
                border: 1px solid {tokens["field_border"]};
                border-radius: 18px;
                padding: 4px;
                outline: none;
            }}
            QListWidget#FileList[empty="true"] {{
                background: transparent;
                border: 1px dashed {tokens["faint"]};
            }}
            QPlainTextEdit#LogBox {{
                background: {tokens["field"]};
                color: {tokens["log_text"]};
                border: 1px solid {tokens["field_border"]};
                border-radius: 16px;
                padding: 8px 10px;
                selection-background-color: {tokens["selection"]};
                selection-color: {tokens["text"]};
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 10px;
                margin: 10px 3px 10px 0;
            }}
            QScrollBar:horizontal {{
                background: transparent;
                height: 10px;
                margin: 0 10px 3px 10px;
            }}
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
                background: {tokens["faint"]};
                border-radius: 3px;
                min-height: 28px;
                min-width: 28px;
            }}
            QScrollBar::add-line, QScrollBar::sub-line {{
                width: 0;
                height: 0;
            }}
            QScrollBar::add-page, QScrollBar::sub-page {{
                background: transparent;
            }}
            QFrame#MetadataPage, QFrame#SettingsPage, QScrollArea#SettingsScroll, QWidget#SettingsContent {{
                background: {tokens["sheet"]};
                border: none;
            }}
            QFrame#ToolRow {{
                background: {tokens["field"]};
                border: 1px solid {tokens["field_border"]};
                border-radius: 18px;
            }}
            QLabel#ToolTitle {{
                color: {tokens["text"]};
                background: transparent;
                border: none;
            }}
            QFrame#ToolRow QLabel {{
                border: none;
            }}
            QTextBrowser#AboutText {{
                background: transparent;
                border: none;
                color: {tokens["text"]};
            }}
            QLabel#DialogMuted {{
                color: {tokens["muted"]};
                background: transparent;
            }}
            QListWidget#ProfileList {{
                background: transparent;
                border: none;
                color: {tokens["text"]};
                outline: none;
            }}
            QListWidget#ProfileList::item {{
                padding: 7px 4px;
                border-radius: 10px;
            }}
            QListWidget#ProfileList::item:selected {{
                background: {tokens["field"]};
                color: {tokens["text"]};
            }}
            QTableWidget#MetaTable {{
                background: {tokens["field"]};
                alternate-background-color: {tokens["card"]};
                color: {tokens["text"]};
                border: 1px solid {tokens["field_border"]};
                border-radius: 14px;
                gridline-color: {tokens["field_border"]};
                selection-background-color: {tokens["selection"]};
                selection-color: {tokens["text"]};
            }}
            QHeaderView::section {{
                background: {tokens["card"]};
                color: {tokens["muted"]};
                border: none;
                border-bottom: 1px solid {tokens["field_border"]};
                padding: 6px 8px;
            }}
            QComboBox, QLineEdit {{
                background: {tokens["field"]};
                color: {tokens["text"]};
                border: 1px solid {tokens["field_border"]};
                border-radius: 10px;
                padding: 5px 10px;
                min-height: 20px;
            }}
            QComboBox:disabled, QLineEdit:disabled {{
                color: {tokens["disabled_text"]};
            }}
            QComboBox QAbstractItemView {{
                background: {tokens["menu_bg"]};
                color: {tokens["text"]};
                selection-background-color: {tokens["menu_hover"]};
                selection-color: {tokens["text"]};
            }}
            QTextBrowser#MetaText {{
                background: {tokens["field"]};
                color: {tokens["text"]};
                border: 1px solid {tokens["field_border"]};
                border-radius: 14px;
                padding: 10px;
            }}
            QToolTip {{
                background: {BRAND_NAVY};
                color: #FFFFFF;
                border: none;
                padding: 6px 8px;
            }}
            """
        )
        for page in (getattr(self, "metadata_page", None), getattr(self, "settings_page", None)):
            if page is not None:
                page.set_theme(tokens)
        log_palette = self.log_box.palette()
        log_palette.setColor(QPalette.PlaceholderText, QColor(tokens["faint"]))
        self.log_box.setPalette(log_palette)
        for widget in self.themed_widgets:
            widget.set_theme(tokens)
        self.theme_button.set_mode(self.theme_mode)

    def _system_prefers_dark(self) -> bool:
        app = QApplication.instance()
        if app is None:
            return False

        style_hints = app.styleHints()
        if hasattr(style_hints, "colorScheme"):
            scheme = style_hints.colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return True
            if scheme == Qt.ColorScheme.Light:
                return False

        return app.palette().color(QPalette.Window).lightness() < 128

    def _connect_system_theme_listener(self) -> None:
        style_hints = QApplication.instance().styleHints()
        if hasattr(style_hints, "colorSchemeChanged"):
            style_hints.colorSchemeChanged.connect(self._handle_system_theme_changed)

    def _handle_system_theme_changed(self, *_args) -> None:
        if self.theme_mode == "system":
            self._transition_theme(self._system_prefers_dark())

    def set_theme_mode(self, mode: str) -> None:
        self.theme_mode = mode
        self.settings.set("theme", mode)
        self.theme_button.set_mode(mode)
        self._transition_theme(self._system_prefers_dark() if mode == "system" else mode == "dark")

    def _transition_theme(self, dark: bool) -> None:
        """Apply the theme; when the appearance really changes, reveal it with the circular mask."""
        if dark == self.dark_theme:
            return
        animate = self.isVisible() and not prefers_reduced_motion()
        # Grabbing the window includes a reveal that may still be running, so a rapid second
        # switch continues from exactly what is on screen.
        snapshot = self.grab() if animate else None
        self.theme_reveal.stop()
        self.dark_theme = dark
        self._apply_styles()
        if snapshot is not None:
            # Darkness expands from the centre; going light, it collapses into the centre.
            self.theme_reveal.start(snapshot, expand=dark)

    # ------------------------------------------------------------------ language

    def set_language(self, locale: str) -> None:
        self.settings.set("language", locale)
        set_locale(locale)  # listeners call retranslate_ui

    def retranslate_ui(self) -> None:
        for setter, key in self._texts:
            setter(tr(key))
        for widget in (self.mode_switch, self.theme_button, self.language_button, self.wordmark.credit,
                       self.aspect_combo, self.intensity_selector, self.log_panel.header):
            widget.retranslate_ui()
        self._sync_mode_ui()
        self._update_selection_labels()
        self._update_output_button()
        self._tools_changed()
        if self.metadata_page is not None:
            self.metadata_page.retranslate_ui()
        if self.settings_page is not None:
            self.settings_page.retranslate_ui()
        # Custom-painted widgets resolve their texts while painting.
        for widget in self.findChildren(QWidget):
            widget.updateGeometry()
            widget.update()

    # ------------------------------------------------------------------ media mode & metadata

    def set_media_mode(self, mode: str) -> None:
        if mode == self.media_mode:
            self.mode_switch.set_mode(mode)
            return
        if self.run_state in ("running", "stopping"):
            self.mode_switch.set_mode(self.media_mode)
            self._report_notice("notice.wait_mode")
            return
        state = self.mode_state[self.media_mode]
        state.update(files=self.selected_videos, size=self.selected_size, origin=self.selection_origin,
                     last_dir=self.last_input_dir)
        self.media_mode = mode
        self.mode_switch.set_mode(mode)
        state = self.mode_state[mode]
        self.last_input_dir = state["last_dir"]
        if state["files"]:
            self._set_selected_videos(state["files"], state["origin"])
        else:
            self.selected_videos, self.selected_size, self.selection_origin = [], 0, ("", {})
            self.file_list.set_videos([])
        if self.run_state == "finished":
            self.run_state = "idle"
        self._sync_mode_ui()
        self._settings_changed()

    def _sync_mode_ui(self) -> None:
        photo = self.media_mode == "photo"
        self.files_title.set_text(self._fit_title(tr(f"section.files.{self.media_mode}").upper()))
        self.options_title.set_text(self._fit_title(tr("section.processing" if photo else "section.format").upper()))
        self.option_pages.setCurrentIndex(1 if photo else 0)
        self._update_selection_labels()
        self.source_button.setText(tr(f"files.choose.{self.media_mode}"))
        self.source_button.setToolTip(tr("files.choose_tooltip"))
        self.source_button.updateGeometry()
        self.source_folder_button.setToolTip(tr(f"files.folder_tooltip.{self.media_mode}"))
        self.file_list.empty_title_key = "files.empty.photo" if photo else "files.empty.video"
        self.file_list.viewport().update()
        self._refresh_profile_field()

    def _update_selection_labels(self) -> None:
        count = len(self.selected_videos)
        if count:
            self.video_count.set_text(tr(f"files.count.{self.media_mode}", n=count).upper())
            key, params = self.selection_origin
            origin = tr(key, **params) if key else ""
            self.video_meta.setText(f"{format_size(self.selected_size)} · {origin}" if origin else format_size(self.selected_size))
        else:
            self.video_count.set_text(tr(f"files.none.{self.media_mode}").upper())
            self.video_meta.setText(tr(f"files.kinds_hint.{self.media_mode}"))

    def _update_output_button(self) -> None:
        self.output_button.setText(tr("output.change") if self.output_dir else tr("output.choose"))
        self.output_button.updateGeometry()

    def _update_intensity(self, key: str) -> None:
        self.photo_intensity = key
        self._settings_changed()

    def _active_profile(self, media: str | None = None) -> dict:
        media = media or self.media_mode
        return self.profile_store.get(self.profile_store.active_id(media))

    def _refresh_profile_field(self) -> None:
        profile = self._active_profile()
        neutral = profile.get("kind") == "none"
        detail = profile_group(profile) if not neutral else tr("meta.field.neutral_detail")
        if not neutral and not exiftool_available():
            detail = tr("meta.field.no_exiftool")
        self.profile_field.set_profile(profile_name(profile), detail, neutral)

    def _close_pages(self) -> None:
        for page in (self.metadata_page, self.settings_page):
            if page is not None:
                page.close_page()

    def open_metadata_dialog(self) -> None:
        # Opens inside this window as a full page instead of a separate window.
        if self.metadata_page is not None:
            self.metadata_page.raise_()
            return
        if self.settings_page is not None:
            self.settings_page.close_page()
        tokens = THEMES["dark" if self.dark_theme else "light"]
        self.metadata_page = MetadataPage(self.profile_store, self.media_mode, tokens, self)
        self.metadata_page.closed.connect(self._metadata_page_closed)
        self.metadata_page.open_in(self.rect())

    def _metadata_page_closed(self) -> None:
        page, self.metadata_page = self.metadata_page, None
        if page is None:
            return
        for media, profile_id in page.applied.items():
            profile = self.profile_store.get(profile_id)
            self._append_log(tr(f"log.profile_applied.{media}", name=profile_name(profile)))
        page.deleteLater()
        self._refresh_profile_field()
        self._settings_changed()

    def open_settings_page(self) -> None:
        if self.settings_page is not None:
            self.settings_page.raise_()
            return
        if self.metadata_page is not None:
            self.metadata_page.raise_()
            return
        tokens = THEMES["dark" if self.dark_theme else "light"]
        self.settings_page = SettingsPage(self.controller, tokens, self)
        self.settings_page.closed.connect(self._settings_page_closed)
        self.settings_page.open_in(self.rect())

    def _settings_page_closed(self) -> None:
        page, self.settings_page = self.settings_page, None
        if page is not None:
            self.controller.changed.disconnect(page.render)
            page.deleteLater()

    # ------------------------------------------------------------------ components

    def _tools_changed(self) -> None:
        controller = self.controller
        ffmpeg_ok, exiftool_ok = controller.ok("ffmpeg"), controller.ok("exiftool")
        if controller.busy == "install":
            pill = ("check", tr("status.installing"))
        elif not controller.detected_once:
            pill = ("check", tr("status.checking"))
        elif ffmpeg_ok and exiftool_ok:
            pill = ("ok", tr("status.ready"))
        elif ffmpeg_ok or exiftool_ok:
            pill = ("warn", tr("status.partial"))
        else:
            pill = ("error", tr("status.setup"))
        self.status_pill.set_state(*pill)
        self.status_pill.setToolTip(tr("status.tooltip"))
        self._refresh_profile_field()
        self._refresh_panel()
        if controller.detected_once and not controller.detecting and not controller.busy:
            self._log_tool_summary()
            if self._offer_pending:
                self._offer_pending = False
                if controller.missing() and controller.supported_platform():
                    QTimer.singleShot(250, self._offer_install)

    def _log_tool_summary(self) -> None:
        summary = []
        for tool in dependencies.TOOLS:
            status = self.controller.status(tool)
            summary.append(f"{TOOL_TITLES[tool]} {status.version}" if status.ok else f"{TOOL_TITLES[tool]}: {tr(f'deps.state.{status.state}')}")
        line = tr("log.components", summary=" · ".join(summary))
        if line != getattr(self, "_last_tool_line", ""):
            self._last_tool_line = line
            self._append_log(line)

    def _offer_install(self) -> None:
        missing = self.controller.missing()
        if not missing or self.settings.get("first_run_offer_done"):
            return
        self.settings.set("first_run_offer_done", True)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(tr("offer.title"))
        box.setText(tr("offer.text", names=", ".join(TOOL_TITLES[t] for t in missing)))
        box.setInformativeText(tr("offer.details"))
        # "&" marks a keyboard mnemonic on native buttons; double it to show a literal ampersand.
        install = box.addButton(tr("settings.install_all").replace("&", "&&"), QMessageBox.AcceptRole)
        box.addButton(tr("offer.later"), QMessageBox.RejectRole)
        box.exec()
        self.open_settings_page()
        if box.clickedButton() is install:
            self.controller.install()

    # ------------------------------------------------------------------ panel

    def _settings_changed(self) -> None:
        # Changing the setup after a run returns the panel from the summary to "ready".
        if self.run_state == "finished":
            self.run_state = "idle"
        self.notice = None
        self._refresh_panel()

    def _tools_ready_for_mode(self) -> bool:
        return self.media_mode == "photo" or self.controller.ok("ffmpeg")

    def _refresh_panel(self) -> None:
        count = len(self.selected_videos)
        total = self.run_total
        state = self.run_state
        mode = self.media_mode
        percent = int(self.run_attempted / total * 100) if total else 0
        controller = self.controller
        tools_ready = self._tools_ready_for_mode()
        ready = tools_ready and count > 0 and self.output_dir is not None
        profile = self._active_profile()
        notice = ""
        hint = ""
        show_open = False
        progress = 0
        done_text = f"0/{count}"
        errors = len(self.run_errors) if state != "idle" else 0

        if state in ("running", "stopping"):
            running = state == "running"
            chip = ("ok", tr("panel.chip.running")) if running else ("check", tr("panel.chip.stopping"))
            metric = f"{percent}%"
            if running:
                position = min(self.run_attempted + 1, total)
                caption = (tr("panel.caption.position", position=position, total=total, name=self.current_file)
                           if self.current_file else tr("panel.caption.starting"))
            else:
                caption = tr("panel.caption.waiting_ffmpeg")
            progress = percent
            done_text = f"{self.run_processed}/{total}"
            notice = self.run_errors[-1] if self.run_errors else ""
            hint = tr("panel.hint.saving", path=compact_path(self.run_output_dir)) if self.run_output_dir else ""
            self.start_button.set_mode("stop", tr("action.stop"))
            self.start_button.setEnabled(running)
        elif state == "finished":
            metric = f"{self.run_processed}/{total}"
            if self.run_stopped:
                chip = ("check", tr("panel.chip.stopped"))
                caption = tr("panel.caption.stopped_saved") if self.run_processed else tr("panel.caption.stopped")
            elif self.run_errors:
                chip = ("error", tr("panel.chip.errors"))
                caption = tr("panel.caption.failed", n=len(self.run_errors), total=total)
                notice = self.run_errors[-1]
            elif self.run_warnings:
                chip = ("error", tr("panel.chip.warnings"))
                caption = tr("panel.caption.warnings")
                notice = self.run_warnings[-1]
            else:
                chip = ("ok", tr("panel.chip.finished"))
                caption = tr(f"panel.caption.done.{mode}", n=total)
            progress = percent
            done_text = f"{self.run_processed}/{total}"
            show_open = self.run_processed > 0 and self.run_output_dir is not None
            hint = compact_path(self.run_output_dir) if self.run_output_dir else ""
            self.start_button.set_mode("start", tr("action.again"))
            self.start_button.setEnabled(ready)
        else:
            self.start_button.set_mode("start", tr("action.start"))
            self.start_button.setEnabled(ready)
            if not controller.detected_once:
                chip, metric, caption = ("check", tr("status.checking")), "…", tr("panel.caption.checking_tools")
            elif not tools_ready:
                chip, metric, caption = ("error", tr("panel.chip.no_ffmpeg")), "!", tr("panel.caption.video_unavailable")
                notice = tr("panel.notice.install_ffmpeg")
                self.start_button.set_mode("setup", tr("action.open_components"))
                self.start_button.setEnabled(not controller.busy)
            elif count == 0:
                chip, metric, caption = ("check", tr("panel.chip.waiting")), "0", tr(f"panel.caption.none.{mode}")
                hint = tr(f"panel.hint.step1.{mode}")
            elif self.output_dir is None:
                chip, metric = ("check", tr("panel.chip.almost")), str(count)
                caption = tr(f"panel.caption.selected.{mode}", n=count)
                hint = tr("panel.hint.step2")
            else:
                chip, metric = ("ok", tr("panel.chip.ready")), str(count)
                if mode == "photo":
                    caption = tr("panel.caption.ready.photo", n=count, option=intensity_label(self.photo_intensity).lower())
                else:
                    caption = tr("panel.caption.ready.video", n=count, option=ASPECT_LABELS[self.aspect_name][0])
                hint = tr("panel.hint.will_save", path=compact_path(self.output_dir))

        if self.notice:
            key, params = self.notice
            notice = tr(key, **params)
            if state == "idle":
                chip = ("error", tr("panel.chip.check_settings"))
        short, caption_key = ASPECT_LABELS[self.aspect_name]
        if mode == "photo":
            option_row = (tr("recap.processing").upper(), intensity_label(self.photo_intensity))
        else:
            option_row = (tr("recap.format").upper(), f"{short} · {tr(caption_key)}")
        self.recap.set_rows(
            [
                (tr("recap.sources").upper(),
                 tr(f"recap.sources_value.{mode}", n=count, size=format_size(self.selected_size)) if count else tr("recap.not_selected")),
                option_row,
                (tr("recap.metadata").upper(), profile_name(profile) if profile.get("kind") != "none" else tr("recap.metadata_removed")),
                (tr("recap.folder").upper(),
                 (self.output_dir.name or str(self.output_dir)) if self.output_dir else tr("recap.folder_none")),
            ]
        )
        self.state_chip.set_state(*chip)
        self.metric_label.set_text(metric)
        self.metric_caption.setText(caption)
        self.progress_bar.setValue(progress)
        self.stat_done.set_value(done_text)
        self.stat_time.set_value(format_seconds(self.run_elapsed if state != "idle" else 0))
        self.stat_errors.set_value(str(errors))
        self.notice_box.set_message(notice)
        self.hint_label.setText(hint)
        self.open_folder_button.setVisible(show_open)

    def _tick(self) -> None:
        if self.run_state in ("running", "stopping"):
            self.run_elapsed = time.perf_counter() - self.start_time
            self.stat_time.set_value(format_seconds(self.run_elapsed))

    def _primary_action(self) -> None:
        if self.run_state == "running":
            self.stop_processing()
        elif self.start_button.mode == "setup":
            self.open_settings_page()
        else:
            self.start_processing()

    def _report_notice(self, key: str, /, **params) -> None:
        self._append_log(tr(key, **params))
        self.notice = (key, params)
        self._refresh_panel()

    def open_output_dir(self) -> None:
        target = self.run_output_dir or self.output_dir
        if target and target.is_dir():
            open_folder(target)

    def _update_aspect_from_combo(self, _index: int | None = None) -> None:
        self.aspect_name = str(self.aspect_combo.currentData())
        if hasattr(self, "state_chip"):
            self._settings_changed()

    # ------------------------------------------------------------------ selection

    def _input_dialog_dir(self) -> str:
        if self.last_input_dir and self.last_input_dir.is_dir():
            return str(self.last_input_dir)
        return default_dialog_dir()

    def _set_selected_videos(self, videos: list[Path], origin: tuple[str, dict]) -> None:
        self.selected_videos = videos
        self.selection_origin = origin
        entries = []
        total_size = 0
        for path in videos:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            total_size += size
            entries.append((path, size))
        self.selected_size = total_size
        self.file_list.set_videos(entries)
        self._update_selection_labels()
        self._settings_changed()

    def _media_extensions(self) -> tuple[set, str]:
        if self.media_mode == "photo":
            return set(PHOTO_EXTENSIONS), tr("files.kinds.photo")
        return VIDEO_EXTENSIONS, tr("files.kinds.video")

    def choose_source_files(self) -> None:
        photo = self.media_mode == "photo"
        selected, _filter = QFileDialog.getOpenFileNames(
            self,
            tr(f"dialog.choose_files.{self.media_mode}"),
            self._input_dialog_dir(),
            photo_filter() if photo else video_filter(),
        )
        if not selected:
            return

        extensions, kinds = self._media_extensions()
        paths = [Path(item) for item in selected]
        videos = collect_videos(paths, extensions)
        skipped = len({path.resolve() for path in paths}) - len(videos)
        if skipped:
            self._append_log(tr("log.skipped", kinds=kinds, n=skipped))
        if not videos:
            self._report_notice("notice.no_matching_files", kinds=kinds)
            return

        self.last_input_dir = videos[0].parent
        folders = {path.parent for path in videos}
        if len(folders) == 1:
            origin = ("files.origin.folder", {"name": videos[0].parent.name or "/"})
        else:
            origin = ("files.origin.folders", {"n": len(folders)})
        self._set_selected_videos(videos, origin)
        self._append_log(tr(f"log.selected.{self.media_mode}", n=len(videos)))

    def choose_source_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, tr(f"dialog.choose_folder.{self.media_mode}"), self._input_dialog_dir())
        if not selected:
            return
        source_dir = Path(selected)
        extensions, kinds = self._media_extensions()
        try:
            videos = collect_videos(sorted(path for path in source_dir.iterdir() if path.is_file()), extensions)
        except OSError as exc:
            self._report_notice("notice.folder_unreadable", path=str(source_dir), error=str(exc))
            return

        if not videos:
            self._report_notice("notice.folder_empty", path=str(source_dir), kinds=kinds)
            return

        self.last_input_dir = source_dir
        self._set_selected_videos(videos, ("files.origin.whole_folder", {"name": source_dir.name or "/"}))
        self._append_log(tr(f"log.folder_found.{self.media_mode}", path=str(source_dir), n=len(videos)))

    def choose_output_dir(self) -> None:
        start_dir = self.output_dir if self.output_dir and self.output_dir.is_dir() else None
        selected = QFileDialog.getExistingDirectory(
            self, tr("dialog.choose_output"), str(start_dir) if start_dir else default_dialog_dir()
        )
        if not selected:
            return
        self.output_dir = Path(selected)
        self.output_path_label.set_path(self.output_dir)
        self._update_output_button()
        self._append_log(tr("log.output_dir", path=str(self.output_dir)))
        self._settings_changed()

    # ------------------------------------------------------------------ processing

    def _set_current_process(self, process: subprocess.Popen | None) -> None:
        with self.process_lock:
            self.current_process = process

    def stop_processing(self) -> None:
        if not (self.worker and self.worker.is_alive()):
            self._append_log(tr("log.nothing_running"))
            return

        self.stop_event.set()
        self.run_state = "stopping"
        self._refresh_panel()
        self._append_log(tr("log.stopping"))

        with self.process_lock:
            process = self.current_process

        if process and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass

    def start_processing(self) -> None:
        if self.worker and self.worker.is_alive():
            self._append_log(tr("log.already_running"))
            return

        mode = self.media_mode
        if not self._tools_ready_for_mode():
            self._report_notice("panel.notice.install_ffmpeg")
            return

        if not self.selected_videos:
            self._report_notice(f"notice.select_first.{mode}")
            return

        profile = self._active_profile()
        if profile.get("kind") != "none" and not exiftool_available():
            self._report_notice("notice.profile_needs_exiftool")
            return

        if self.output_dir is None:
            self._report_notice("notice.select_output")
            return

        if not self.output_dir.is_dir():
            self._report_notice("notice.output_unavailable", path=str(self.output_dir))
            return

        if not os.access(self.output_dir, os.W_OK):
            self._report_notice("notice.output_not_writable", path=str(self.output_dir))
            return

        videos = []
        for path in self.selected_videos:
            if path.is_file():
                videos.append(path)
            else:
                self._append_log(tr("log.file_missing", path=str(path)))

        if not videos:
            self._report_notice(f"notice.files_gone.{mode}")
            return

        self.stop_event.clear()
        self.start_time = time.perf_counter()
        self.notice = None
        self.run_state = "running"
        self.run_total = len(videos)
        self.run_processed = 0
        self.run_attempted = 0
        self.run_elapsed = 0.0
        self.run_errors = []
        self.run_warnings = []
        self.run_stopped = False
        self.run_output_dir = self.output_dir
        self.current_file = ""
        self._set_controls_enabled(False)
        self._refresh_panel()
        self._append_log(tr(f"log.start.{mode}", n=len(videos), path=str(self.output_dir)))
        if profile.get("kind") != "none":
            self._append_log(tr("log.profile", name=profile_name(profile)))

        self.worker = threading.Thread(
            target=self._process_videos,
            args=(videos, self.output_dir, self.aspect_name, mode, profile, self.photo_intensity),
            daemon=True,
        )
        self.worker.start()

    def _process_videos(
        self,
        videos: list[Path],
        output_dir: Path,
        aspect_name: str,
        mode: str = "video",
        profile: dict | None = None,
        intensity: str = "medium",
    ) -> None:
        profile = profile or {"kind": "none", "fields": {}}
        processed = 0
        attempted = 0
        total = len(videos)
        stopped = False

        for index, input_path in enumerate(videos, start=1):
            if self.stop_event.is_set():
                stopped = True
                break

            self.log_queue.put(("current", input_path.name))
            self.log_queue.put(("log", tr("log.processing", index=index, total=total, name=input_path.name)))
            output_path: Path | None = None

            try:
                if mode == "photo":
                    output_path, done_message = self._process_one_photo(input_path, output_dir, profile, intensity)
                else:
                    output_path, done_message = self._process_one_video(input_path, output_dir, aspect_name, profile)
            except ProcessingStopped:
                stopped = True
                remove_partial_output(output_path)
                self.log_queue.put(("log", tr("log.stopped_by_user")))
                break
            except Exception as exc:
                remove_partial_output(output_path)
                if self.stop_event.is_set():
                    stopped = True
                    self.log_queue.put(("log", tr("log.stopped_by_user")))
                    break
                self.log_queue.put(("error", f"{input_path.name}: {exc}"))
                self.log_queue.put(("error_log", tr("log.error", name=input_path.name, error=str(exc))))
            else:
                processed += 1
                self.log_queue.put(("log", done_message))

            attempted += 1
            elapsed = time.perf_counter() - self.start_time
            self.log_queue.put(("status", (processed, total, elapsed, attempted)))

        elapsed = time.perf_counter() - self.start_time
        key = "log.summary_stopped" if stopped else "log.summary_done"
        self.log_queue.put(("log", tr(key, processed=processed, total=total)))
        self.log_queue.put(("status", (processed, total, elapsed, attempted)))
        self._set_current_process(None)
        self.log_queue.put(("done", stopped))

    def _process_one_video(self, input_path: Path, output_dir: Path, aspect_name: str, profile: dict) -> tuple[Path, str]:
        neutral = profile.get("kind") == "none"
        container = video_container(profile)
        output_path = random_output_path(output_dir, "video", "." + container)
        try:
            plan = None if neutral else build_plan(profile, "video", container, input_path)
            trim_start, trim_end = run_ffmpeg(
                input_path,
                output_path,
                aspect_name,
                stop_event=self.stop_event,
                process_callback=self._set_current_process,
                encode_options=ffmpeg_video_options(profile, plan),
                keep_metadata=bool(profile.get("keep_source_metadata")),
            )
            message = tr("log.video_done", name=output_path.name, start=f"{trim_start:.2f}", end=f"{trim_end:.2f}")
            if plan is not None:
                # Tokens such as $FPS need the finished file; the time stays the same as in FFmpeg.
                fps = probe_fps(get_ffmpeg_path(), output_path)
                plan = build_plan(profile, "video", container, input_path, fps=fps, moment=(plan.moment, plan.moment_note))
                for note in write_video_metadata(profile, plan, input_path, output_path):
                    self.log_queue.put(("log", note))
                message += self._report_verification(output_path, plan)
            return output_path, message
        except BaseException:
            remove_partial_output(output_path)
            raise

    def _process_one_photo(self, input_path: Path, output_dir: Path, profile: dict, intensity: str) -> tuple[Path, str]:
        fmt = photo_format(input_path)
        output_path = random_output_path(output_dir, "photo", OUTPUT_EXTENSIONS.get(fmt, input_path.suffix.lower()))
        try:
            info = process_photo(input_path, output_path, intensity)
            message = tr("log.photo_done", name=output_path.name, width=info["width"], height=info["height"])
            if info["format"] != fmt:
                message += tr("log.photo_content", format=info["format"].upper())
            if exiftool_available():
                source_meta = read_metadata(input_path)
                plan = build_plan(profile, "photo", info["format"], input_path, source_meta=source_meta)
                for note in write_photo_metadata(profile, plan, input_path, output_path, source_meta):
                    self.log_queue.put(("log", note))
                if plan.fields:
                    message += self._report_verification(output_path, plan)
            return output_path, message
        except PhotoError as exc:
            remove_partial_output(output_path)
            raise RuntimeError(str(exc)) from exc
        except BaseException:
            remove_partial_output(output_path)
            raise

    def _report_verification(self, output_path: Path, plan) -> str:
        report = verify_output(plan, output_path, get_ffmpeg_path())
        for key, reason in plan.skipped:
            self.log_queue.put(("log", tr("log.meta_skipped", key=key, reason=reason)))
        if not report.ok:
            warning = f"{output_path.name}: {report.summary()}"
            self.log_queue.put(("warning", warning))
            self.log_queue.put(("log", tr("log.warning", warning=warning)))
        return f" | {report.summary()}"

    def _drain_log_queue(self) -> None:
        changed = False
        try:
            while True:
                event, payload = self.log_queue.get_nowait()

                if event == "log":
                    self._append_log(str(payload))
                elif event == "error_log":
                    self._append_log(str(payload), is_error=True)
                elif event == "current":
                    self.current_file = str(payload)
                    changed = True
                elif event == "error":
                    self.run_errors.append(str(payload))
                    changed = True
                elif event == "warning":
                    self.run_warnings.append(str(payload))
                    changed = True
                elif event == "status":
                    processed, total, elapsed, attempted = payload
                    self.run_processed = int(processed)
                    self.run_total = int(total)
                    self.run_elapsed = float(elapsed)
                    self.run_attempted = int(attempted)
                    changed = True
                elif event == "done":
                    self.run_stopped = bool(payload)
                    self.run_state = "finished"
                    self.current_file = ""
                    self._set_controls_enabled(True)
                    changed = True
        except queue.Empty:
            pass
        if changed:
            self._refresh_panel()

    def _append_log(self, message: str, is_error: bool = False) -> None:
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        self.log_box.appendPlainText(line)
        self.log_box.verticalScrollBar().setValue(self.log_box.verticalScrollBar().maximum())
        self.log_panel.note(message, is_error)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.source_button.setEnabled(enabled)
        self.source_folder_button.setEnabled(enabled)
        self.output_button.setEnabled(enabled)
        self.aspect_combo.setEnabled(enabled)
        self.intensity_selector.setEnabled(enabled)
        self.metadata_button.setEnabled(enabled)
        self.mode_switch.setEnabled(enabled)


def configure_application(app: QApplication, settings: AppSettings) -> None:
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)
    app.setOrganizationDomain(BUNDLE_ID.rsplit(".", 1)[0])
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName(BUNDLE_ID)
    app.setStyle("Fusion")
    register_fonts()
    icon_path = resource_path("assets/app_icon.png")
    if should_set_runtime_icon() and icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    app.setFont(ui_font(13))
    saved = settings.get("language")
    set_locale(saved if saved in SUPPORTED_LOCALES else system_locale())


def main() -> None:
    if "--self-test" in sys.argv:
        # CI check of a packaged build: see uniqlab/selftest.py. Never used in normal launches.
        from uniqlab import selftest

        args = sys.argv[sys.argv.index("--self-test") + 1:]
        report = args[0] if args and not args[0].startswith("--") else "uniqlab-selftest.json"
        sys.exit(selftest.run(report, install_tools="--install-tools" in sys.argv))
    try:
        app = QApplication(sys.argv)
        settings = AppSettings()
        configure_application(app, settings)
        window = UniqLabWindow(settings)
        window.show()
        sys.exit(app.exec())
    except Exception as exc:
        log_path = write_error_log(exc)
        try:
            QMessageBox.critical(None, tr("error.startup_title"), tr("error.startup_text", path=str(log_path)))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
