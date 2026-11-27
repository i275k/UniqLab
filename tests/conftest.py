"""Test setup: headless Qt, an isolated per-session data folder, and helpers for optional tools."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("UNIQLAB_REDUCE_MOTION", "1")
# CI installs FFmpeg/ExifTool into UNIQLAB_DATA_DIR through the app's own dependency manager;
# locally a throwaway folder keeps the real user settings untouched.
if "UNIQLAB_DATA_DIR" not in os.environ:
    os.environ["UNIQLAB_DATA_DIR"] = tempfile.mkdtemp(prefix="uniqlab-test-")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def english():
    from uniqlab import i18n

    previous = i18n.current_locale()
    i18n.set_locale("en")
    yield
    i18n.set_locale(previous)


@pytest.fixture(scope="session")
def tools():
    """Paths of working FFmpeg and ExifTool, or skip the test."""
    from uniqlab import dependencies

    found = {tool: dependencies.detect(tool) for tool in dependencies.TOOLS}
    missing = [tool for tool, status in found.items() if not status.ok]
    if missing:
        pytest.skip("tools not available: " + ", ".join(missing))
    from uniqlab import app as app_module
    from uniqlab.media_metadata import set_exiftool_path

    app_module._TOOL_PATHS["ffmpeg"] = found["ffmpeg"].path
    app_module._TOOL_PATHS["exiftool"] = found["exiftool"].path
    set_exiftool_path(found["exiftool"].path)
    return {tool: status.path for tool, status in found.items()}
