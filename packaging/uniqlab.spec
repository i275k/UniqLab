# PyInstaller spec for UniqLab (onedir build: Qt stays replaceable as required by the LGPL).
# Build from the repository root:  pyinstaller --noconfirm --clean packaging/uniqlab.spec
# FFmpeg, FFprobe and ExifTool are NOT bundled; the app downloads them on the user's request.
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(ROOT))
from uniqlab import APP_NAME, BUNDLE_ID, __version__  # noqa: E402

datas = [
    (str(ROOT / "uniqlab" / "assets" / "app_icon.png"), "uniqlab/assets"),
    (str(ROOT / "uniqlab" / "assets" / "fonts"), "uniqlab/assets/fonts"),
    (str(ROOT / "uniqlab" / "i18n" / "locales"), "uniqlab/i18n/locales"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
    (str(ROOT / "licenses"), "licenses"),
]


def _license_files():
    """License texts of the bundled Python runtime and libraries, copied from the build environment."""
    import importlib.metadata as metadata
    import sysconfig

    found = []
    for dist, target in (("pillow", "Pillow-LICENSE.txt"), ("certifi", "certifi-LICENSE.txt"),
                         ("pyinstaller", "PyInstaller-COPYING.txt")):
        for file in metadata.distribution(dist).files or []:
            if any(word in file.name.upper() for word in ("LICENSE", "COPYING")):
                found.append((str(file.locate()), target))
                break
    stdlib = Path(sysconfig.get_paths()["stdlib"])
    for candidate in (Path(sys.base_prefix) / "LICENSE.txt", stdlib / "LICENSE.txt", stdlib.parent / "LICENSE.txt"):
        if candidate.exists():
            found.append((str(candidate), "Python-LICENSE.txt"))
            break
    return found


import shutil  # noqa: E402
import tempfile  # noqa: E402

_license_stage = Path(tempfile.mkdtemp(prefix="uniqlab-licenses-"))
for _source, _name in _license_files():
    shutil.copyfile(_source, _license_stage / _name)
datas.append((str(_license_stage), "licenses"))

# Qt modules UniqLab never uses; leaving them out keeps the bundle smaller.
excludes = [
    "tkinter", "unittest", "pydoc_data", "test",
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtXml", "PySide6.QtDBus", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtConcurrent", "PySide6.QtPrintSupport", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
]

if sys.platform.startswith("linux"):
    # Leave out Qt's GTK3 platform theme. It would pull the build system's whole GTK stack (GTK, GDK, Pango,
    # Cairo, ATK, …) into the AppImage, where it can clash with the user's GTK and adds licence obligations.
    # UniqLab draws its own light/dark theme; file dialogs use the XDG portal or Qt's own dialog instead.
    import os as _os
    from PyInstaller.utils.hooks import qt as _qt_hooks

    _collect_plugins = _qt_hooks.QtLibraryInfo.collect_plugins

    def _collect_plugins_without_gtk3(self, plugin_type):
        return [entry for entry in _collect_plugins(self, plugin_type)
                if not _os.path.basename(entry[0]).startswith("libqgtk3")]

    _qt_hooks.QtLibraryInfo.collect_plugins = _collect_plugins_without_gtk3

windows_version_file = None
if sys.platform.startswith("win"):
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
    )

    numbers = tuple(int(part) for part in __version__.split(".")) + (0,)
    windows_version_file = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "onyx.x (i275k)"),
                StringStruct("FileDescription", APP_NAME),
                StringStruct("FileVersion", __version__),
                StringStruct("InternalName", APP_NAME),
                StringStruct("LegalCopyright", "Copyright (c) 2026 onyx.x (i275k). MIT License."),
                StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", __version__),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    icon = str(ROOT / "packaging" / "macos" / "UniqLab.icns")
elif sys.platform.startswith("win"):
    icon = str(ROOT / "packaging" / "windows" / "UniqLab.ico")
else:
    icon = None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    disable_windowed_traceback=False,
    strip=False,
    upx=False,
    icon=icon,
    version=windows_version_file,
    # macOS: None = ad-hoc signature (no paid certificate, no notarization).
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier=BUNDLE_ID,
        version=__version__,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHumanReadableCopyright": "Copyright (c) 2026 onyx.x (i275k). MIT License.",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "LSApplicationCategoryType": "public.app-category.video",
            "LSMinimumSystemVersion": "13.0",
        },
    )
