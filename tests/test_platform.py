"""Platform layer: data folder, resources, process flags, fonts."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from uniqlab import platform_support as ps


def test_data_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIQLAB_DATA_DIR", str(tmp_path / "data"))
    assert ps.app_data_dir() == tmp_path / "data"


def test_platform_key_known():
    key = ps.platform_key()
    assert "-" in key
    if sys.platform == "darwin" and os.uname().machine == "arm64":
        assert key == "macos-arm64"


def test_resources_exist():
    for relative in ("assets/app_icon.png", *("assets/" + font for font in ps.BUNDLED_FONTS), "i18n/locales/en.json"):
        assert ps.resource_path(relative).exists(), relative


def test_hidden_process_kwargs():
    kwargs = ps.hidden_process_kwargs()
    if sys.platform.startswith("win"):
        assert kwargs.get("creationflags")
    else:
        assert "creationflags" not in kwargs


def test_font_profile_override(monkeypatch):
    monkeypatch.setenv("UNIQLAB_FONT_PROFILE", "bundled")
    assert ps.font_profile() == "bundled"
    monkeypatch.setenv("UNIQLAB_FONT_PROFILE", "apple")
    assert ps.font_profile() == "apple"


def test_reduce_motion_override(monkeypatch):
    monkeypatch.setenv("UNIQLAB_REDUCE_MOTION", "1")
    assert ps.prefers_reduced_motion() in (True, False)


def test_run_captures_output():
    result = ps.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
    assert result.stdout.strip() == "ok"


def test_settings_roundtrip(tmp_path):
    from uniqlab.settings import AppSettings

    settings = AppSettings(tmp_path / "settings.json")
    settings.set("language", "ru")
    assert AppSettings(tmp_path / "settings.json").get("language") == "ru"
    settings.set("language", None)
    assert AppSettings(tmp_path / "settings.json").get("language") is None
    assert not list(Path(tmp_path).glob("*.tmp"))
