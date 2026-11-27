"""Small platform adapters: paths, process launching, accessibility and fonts.

Everything that differs between macOS, Windows and Linux lives here so the rest of the
code base stays platform-neutral.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from uniqlab import APP_NAME

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

PACKAGE_DIR = Path(__file__).resolve().parent


# ------------------------------------------------------------------------- identity


def platform_key() -> str:
    """"macos-arm64", "windows-x64", "linux-x64" … (unsupported combos are still reported)."""
    machine = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine, machine)
    system = "macos" if IS_MACOS else "windows" if IS_WINDOWS else "linux" if IS_LINUX else sys.platform
    return f"{system}-{arch}"


def platform_label() -> str:
    names = {"macos": "macOS", "windows": "Windows", "linux": "Linux"}
    system, _, arch = platform_key().partition("-")
    return f"{names.get(system, system)} · {arch}"


# ------------------------------------------------------------------------- paths


def resource_path(relative: str) -> Path:
    """A bundled resource, both from source and from a PyInstaller build."""
    base = Path(getattr(sys, "_MEIPASS", PACKAGE_DIR.parent))
    candidate = base / "uniqlab" / relative
    return candidate if candidate.exists() else PACKAGE_DIR / relative


def app_data_dir() -> Path:
    """Per-user data folder (profiles, settings, downloaded tools). No admin rights needed."""
    override = os.environ.get("UNIQLAB_DATA_DIR")
    if override:
        return Path(override)
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home()) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME


def desktop_dir() -> Path:
    try:
        from PySide6.QtCore import QStandardPaths

        location = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
        if location and Path(location).is_dir():
            return Path(location)
    except Exception:
        pass
    desktop = Path.home() / "Desktop"
    return desktop if desktop.is_dir() else Path.home()


def executable_name(name: str) -> str:
    return f"{name}.exe" if IS_WINDOWS else name


# ------------------------------------------------------------------------- processes


def hidden_process_kwargs() -> dict:
    """Keep console windows from flashing up on Windows when tools are launched."""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, **hidden_process_kwargs(), **kwargs)


def popen(args: list[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen(args, **hidden_process_kwargs(), **kwargs)


def open_folder(path: Path) -> bool:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def open_url(url: str) -> bool:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    return QDesktopServices.openUrl(QUrl(url))


def which(name: str) -> str | None:
    return shutil.which(executable_name(name)) or shutil.which(name)


# ------------------------------------------------------------------------- accessibility

_MAC_REDUCE_QUERY = None
_REDUCE_CACHE: tuple[float, bool] | None = None


def _mac_reduced_motion() -> bool:
    global _MAC_REDUCE_QUERY
    if _MAC_REDUCE_QUERY is None:
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        send_object = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(address)
        send_bool = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(address)
        _MAC_REDUCE_QUERY = (
            objc.objc_getClass(b"NSWorkspace"),
            objc.sel_registerName(b"sharedWorkspace"),
            objc.sel_registerName(b"accessibilityDisplayShouldReduceMotion"),
            send_object,
            send_bool,
        )
    workspace_class, shared, reduce, send_object, send_bool = _MAC_REDUCE_QUERY
    if not workspace_class:
        return False
    workspace = send_object(workspace_class, shared)
    return bool(workspace) and bool(send_bool(workspace, reduce))


def _windows_reduced_motion() -> bool:
    spi_getclientareaanimation = 0x1042
    enabled = ctypes.c_bool(True)
    ok = ctypes.windll.user32.SystemParametersInfoW(spi_getclientareaanimation, 0, ctypes.byref(enabled), 0)
    return bool(ok) and not enabled.value


def _linux_reduced_motion() -> bool:
    if not shutil.which("gsettings"):
        return False
    result = run(["gsettings", "get", "org.gnome.desktop.interface", "enable-animations"],
                 capture_output=True, text=True, timeout=2)
    return result.returncode == 0 and result.stdout.strip() == "false"


def prefers_reduced_motion() -> bool:
    """The OS "reduce motion" / "show animations" accessibility preference."""
    global _REDUCE_CACHE
    if os.environ.get("UNIQLAB_REDUCE_MOTION") in ("0", "1"):
        return os.environ["UNIQLAB_REDUCE_MOTION"] == "1"
    now = time.monotonic()
    if _REDUCE_CACHE and now - _REDUCE_CACHE[0] < 5:
        return _REDUCE_CACHE[1]
    try:
        value = _mac_reduced_motion() if IS_MACOS else _windows_reduced_motion() if IS_WINDOWS else _linux_reduced_motion()
    except Exception:
        value = False
    _REDUCE_CACHE = (now, value)
    return value


# ------------------------------------------------------------------------- fonts


def font_profile() -> str:
    """"apple": the system SF Pro (not redistributed); "bundled": the OFL fonts shipped with UniqLab."""
    override = os.environ.get("UNIQLAB_FONT_PROFILE")
    if override in ("apple", "bundled"):
        return override
    return "apple" if IS_MACOS else "bundled"


BUNDLED_FONTS = ("fonts/Unbounded-Variable.ttf", "fonts/Inter-Variable.ttf")
BUNDLED_DISPLAY_FAMILY = "Unbounded"
BUNDLED_TEXT_FAMILY = "Inter"
# Used for glyphs the brand fonts do not have (Simplified Chinese).
CJK_FALLBACKS = ("PingFang SC", "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans SC",
                 "Source Han Sans SC", "WenQuanYi Micro Hei")


def register_fonts() -> None:
    from PySide6.QtGui import QFontDatabase

    if font_profile() != "bundled":
        return
    for relative in BUNDLED_FONTS:
        path = resource_path("assets/" + relative)
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
