"""Self-test of an installed or packaged UniqLab, used by CI on the real DMG / installer / AppImage.

    UniqLab --self-test REPORT.json [--install-tools]

Checks bundled resources, every language, the main window, photo processing and — with
``--install-tools`` — downloads the pinned FFmpeg/ExifTool into ``UNIQLAB_DATA_DIR`` and processes a
video and a photo with metadata presets while network access is blocked for the processing step.
Writes a JSON report and exits with 0 (all checks passed) or 1. Not shown anywhere in the UI.
"""

from __future__ import annotations

import json
import platform
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path


def _resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


class _Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def check(self, name: str, func) -> object:
        started = time.time()
        try:
            detail = func()
            shown = detail if isinstance(detail, (dict, list, str, int, float, bool, type(None))) else type(detail).__name__
            self.checks.append({"name": name, "ok": True, "detail": shown, "seconds": round(time.time() - started, 2)})
            return detail
        except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
            self.checks.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}",
                                "trace": traceback.format_exc()[-1500:], "seconds": round(time.time() - started, 2)})
            return None

    @property
    def ok(self) -> bool:
        return all(item["ok"] for item in self.checks)


class _NoNetwork:
    """Blocks new sockets inside this process: proves processing does not use the network."""

    def __enter__(self):
        self._original = socket.socket.connect

        def refuse(*_args, **_kwargs):
            raise OSError("network blocked by self-test")

        socket.socket.connect = refuse
        return self

    def __exit__(self, *_exc):
        socket.socket.connect = self._original


class _HiddenSystemTools:
    """Hides FFmpeg/ExifTool installed on the machine (PATH, Homebrew), so the first checks see a clean
    system on every CI runner. The app's own detection code runs unchanged in its background thread."""

    def __enter__(self):
        from uniqlab import dependencies

        self._module = dependencies
        self._original = dependencies._system_candidates
        dependencies._system_candidates = lambda _tool: []
        return self

    def __exit__(self, *_exc):
        self._module._system_candidates = self._original


def run(report_path: str, install_tools: bool = False) -> int:
    from PySide6.QtWidgets import QApplication

    from uniqlab import APP_NAME, __version__, dependencies, i18n
    from uniqlab.platform_support import font_profile, platform_key, resource_path

    report = _Report()
    work = Path(tempfile.mkdtemp(prefix="uniqlab-selftest-"))
    app = QApplication.instance() or QApplication(sys.argv[:1])

    def resources():
        root = _resource_root()
        required = [resource_path("assets/app_icon.png"), root / "LICENSE", root / "THIRD_PARTY_NOTICES.md",
                    root / "licenses" / "LGPL-3.0.txt", root / "licenses" / "GPL-3.0.txt",
                    root / "licenses" / "Qt-third-party-notices.txt", root / "licenses" / "Apache-2.0.txt"]
        if getattr(sys, "frozen", False):
            required += [root / "licenses" / name for name in ("Pillow-LICENSE.txt", "certifi-LICENSE.txt", "Python-LICENSE.txt")]
        if font_profile() == "bundled":
            required += [resource_path("assets/fonts/Inter-Variable.ttf"), resource_path("assets/fonts/Unbounded-Variable.ttf")]
        missing = [str(path.relative_to(root)) if path.is_relative_to(root) else str(path) for path in required if not path.exists()]
        assert not missing, f"missing: {missing}"
        import certifi

        assert Path(certifi.where()).is_file(), "certifi CA bundle missing"
        return {"files": len(required), "root_is_bundle": getattr(sys, "frozen", False)}

    def languages():
        reference = set(i18n.catalog("en"))
        counts = {}
        for locale in i18n.SUPPORTED_LOCALES:
            keys = set(i18n.catalog(locale))
            assert keys == reference, f"{locale}: {len(reference - keys)} missing keys"
            counts[locale] = len(keys)
        return counts

    report.check("environment", lambda: {
        "app": APP_NAME, "version": __version__, "platform": platform_key(), "machine": platform.machine(),
        "python": platform.python_version(), "frozen": bool(getattr(sys, "frozen", False)),
        "qt_platform": app.platformName(), "font_profile": font_profile(),
    })
    report.check("resources", resources)
    report.check("languages", languages)

    from uniqlab.app import UniqLabWindow, configure_application
    from uniqlab.settings import AppSettings

    settings = AppSettings(work / "settings.json")
    settings.set("first_run_offer_done", True)
    report.check("configure_application", lambda: configure_application(app, settings) or i18n.current_locale())

    def fonts():
        from PySide6.QtGui import QFontDatabase

        families = set(QFontDatabase.families())
        if font_profile() == "bundled":
            assert {"Inter", "Unbounded"} <= families, "bundled fonts not registered"
        return {"profile": font_profile(), "families": len(families)}

    report.check("fonts", fonts)

    def wait_for_detection(timeout: float = 60) -> None:
        controller = window.controller
        deadline = time.time() + timeout
        while (not controller.detected_once or controller.detecting) and time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
        assert controller.detected_once and not controller.detecting, "component detection did not finish"

    def switch_languages(expect_ffmpeg: bool):
        """Every locale must show its own catalog text for the state the window is really in."""
        wait_for_detection()
        controller = window.controller
        assert controller.ok("ffmpeg") == expect_ffmpeg, f"FFmpeg detected: {controller.ok('ffmpeg')}, expected {expect_ffmpeg}"
        ffmpeg_ok, exiftool_ok = controller.ok("ffmpeg"), controller.ok("exiftool")
        start_key = "action.start" if ffmpeg_ok else "action.open_components"
        pill_key = "status.ready" if ffmpeg_ok and exiftool_ok else "status.partial" if ffmpeg_ok or exiftool_ok else "status.setup"
        log_before = window.log_box.blockCount()
        texts = {}
        for locale in (*i18n.SUPPORTED_LOCALES, "en"):
            window.set_language(locale)
            app.processEvents()
            catalog = i18n.catalog(locale)
            shown = {"start": window.start_button.text(), "status": window.status_pill.label,
                     "source": window.source_button.text()}
            expected = {"start": catalog[start_key], "status": catalog[pill_key], "source": catalog["files.choose.video"]}
            assert i18n.current_locale() == locale and settings.get("language") == locale, f"locale not applied: {locale}"
            assert shown == expected, f"{locale}: shown {shown}, expected {expected}"
            # Video mode: without FFmpeg the button opens Settings → Components; with it, it waits for files.
            assert window.start_button.isEnabled() == (not ffmpeg_ok), f"{locale}: start button enabled state"
            texts[locale] = shown["start"]
        assert window.log_box.blockCount() == log_before, "switching the language added log lines"
        return {"ffmpeg": ffmpeg_ok, "exiftool": exiftool_ok, "start_button": texts}

    window = None
    with _HiddenSystemTools():
        window = report.check("main_window", lambda: UniqLabWindow(settings))
        if window is not None:
            window.show()
            app.processEvents()
            # Real asynchronous detection on a system without FFmpeg/ExifTool: in CI the data folder is
            # empty, so nothing is installed yet (a reused folder may already hold UniqLab's own copy).
            already_installed = dependencies.detect("ffmpeg").ok
            report.check("language_switching", lambda: switch_languages(expect_ffmpeg=already_installed))
    if window is not None:
        report.check("open_pages", lambda: (window.open_settings_page(), app.processEvents(), window.settings_page.close_page(),
                                            window.open_metadata_dialog(), app.processEvents(), window.metadata_page.close_page(),
                                            app.processEvents()) and "ok")

    def photo_without_tools():
        from PIL import Image

        from uniqlab.photo_processing import process_photo

        results = {}
        for extension in ("jpg", "png", "webp"):
            source = work / f"\u0444\u043e\u0442\u043e \u5199\u771f \u00e9.{extension}"
            Image.new("RGB", (640, 480), (30, 120, 220)).save(source)
            output = work / f"out-{extension}.{extension}"
            info = process_photo(source, output, "medium")
            assert output.stat().st_size > 0
            results[extension] = info.get("format")
        return results

    report.check("photo_processing_pillow", photo_without_tools)

    if install_tools and window is not None:
        def install():
            installed = {}
            for tool in dependencies.TOOLS:
                release = dependencies.pinned_release(tool)
                assert release is not None, f"no pinned {tool} for {platform_key()}"
                status = dependencies.install(release)
                assert status.ok, f"{tool}: {status.state} {status.detail}"
                installed[tool] = status.version
            for tool in dependencies.TOOLS:
                status = dependencies.detect(tool)
                assert status.ok and status.source == "managed", f"{tool} detected as {status.source}"
            window.controller.detect_all()
            deadline = time.time() + 60
            while window.controller.detecting and time.time() < deadline:
                app.processEvents()
                time.sleep(0.05)
            assert not window.controller.missing(), f"window still misses {window.controller.missing()}"
            return installed

        if report.check("install_tools", install) is not None:
            from uniqlab.metadata_presets import builtin_profile

            report.check("language_switching_with_tools", lambda: switch_languages(expect_ffmpeg=True))

            def process_offline():
                import subprocess

                from uniqlab.app import get_ffmpeg_path
                from uniqlab.platform_support import hidden_process_kwargs

                source = work / "clip \u00fc \u89c6\u9891.mp4"
                subprocess.run([get_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                "testsrc=size=640x360:rate=30:duration=2", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)],
                               check=True, **hidden_process_kwargs())
                avi = work / "clip.avi"
                subprocess.run([get_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                "testsrc2=size=640x360:rate=30:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(avi)],
                               check=True, **hidden_process_kwargs())
                photo = work / "\u0444\u043e\u0442\u043e \u5199\u771f \u00e9.jpg"
                out_dir = work / "\u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 output"
                out_dir.mkdir(exist_ok=True)
                results = {}
                with _NoNetwork():
                    for preset in ("builtin:video-none", "builtin:video-iphone-12-pro", "builtin:video-premiere-2024"):
                        output, message = window._process_one_video(source, out_dir, "9:16", builtin_profile(preset))
                        assert output.exists() and output.stat().st_size > 1000, message
                        assert "mismatch" not in message, message
                        results[preset] = message.strip()
                    output, message = window._process_one_video(avi, out_dir, "1:1", builtin_profile("builtin:video-iphone-12-pro"))
                    assert output.exists() and output.stat().st_size > 1000, message
                    results["avi + builtin:video-iphone-12-pro"] = message.strip()
                    for preset in ("builtin:photo-iphone-15-pro", "builtin:photo-galaxy-s25-ultra"):
                        output, message = window._process_one_photo(photo, out_dir, builtin_profile(preset), "medium")
                        assert output.exists(), message
                        assert "mismatch" not in message, message
                        results[preset] = message.strip()
                return results

            report.check("processing_with_presets_offline", process_offline)

    if window is not None:
        window.close()
    result = {"ok": report.ok, "checks": report.checks}
    Path(report_path).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if report.ok else 1
