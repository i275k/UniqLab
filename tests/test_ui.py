"""Headless UI smoke tests: header layout, language switching, pages, credit link."""

from __future__ import annotations

import time

import pytest

from uniqlab import i18n


def _pump(app, seconds=0.1):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


@pytest.fixture()
def window(qapp, tmp_path, english):
    from uniqlab.app import UniqLabWindow
    from uniqlab.settings import AppSettings

    settings = AppSettings(tmp_path / "settings.json")
    settings.set("first_run_offer_done", True)
    win = UniqLabWindow(settings)
    win.show()
    _pump(qapp, 0.3)
    yield win
    win.close()
    win.deleteLater()
    _pump(qapp, 0.05)


def test_header_order(window):
    layout = window.wordmark.parentWidget().layout()
    widgets = [layout.itemAt(i).widget() for i in range(layout.count()) if layout.itemAt(i).widget()]
    order = [widgets.index(w) for w in (window.mode_switch, window.theme_button, window.language_button, window.status_pill)]
    assert order == sorted(order)
    # The language selector sits immediately right of the theme toggle.
    assert widgets.index(window.language_button) == widgets.index(window.theme_button) + 1


def test_branding(window):
    assert window.windowTitle() == "UniqLab"
    assert window.wordmark.credit.TEXT == "by onyx.x (i275k)"


def test_credit_opens_github(window, monkeypatch):
    opened = []
    monkeypatch.setattr("uniqlab.app.open_url", opened.append)
    window.wordmark.credit.clicked.disconnect()
    from uniqlab import app as app_module

    window.wordmark.credit.clicked.connect(lambda: app_module.open_url(app_module.AUTHOR_URL))
    window.wordmark.credit.click()
    assert opened == ["https://github.com/i275k"]


@pytest.mark.parametrize("locale", i18n.SUPPORTED_LOCALES)
def test_language_switch_without_restart(window, qapp, locale):
    window.set_language(locale)
    _pump(qapp)
    assert i18n.current_locale() == locale
    assert window.source_button.text() == i18n.tr("files.choose.video")
    assert window.start_button.text() in (i18n.tr("action.start"), i18n.tr("action.open_components"))
    assert window.settings.get("language") == locale
    assert window.language_button.toolTip() == i18n.tr("language.tooltip", language=i18n.NATIVE_NAMES[locale])


def test_media_mode_switch(window, qapp):
    window.set_media_mode("photo")
    _pump(qapp)
    assert window.option_pages.currentIndex() == 1
    assert window.files_title.text() == i18n.tr("section.files.photo").upper()
    window.set_media_mode("video")
    assert window.option_pages.currentIndex() == 0


def test_pages_open_and_retranslate(window, qapp):
    window.open_settings_page()
    _pump(qapp, 0.3)
    assert window.settings_page is not None and window.settings_page.isVisible()
    window.set_language("ru")
    _pump(qapp)
    assert window.settings_page.install_button.text() in {
        i18n.tr("settings.install_all"), i18n.tr("settings.all_installed"), i18n.tr("settings.cancel"),
    } or window.settings_page.install_button.text().startswith("Установить")
    window.settings_page.close_page()
    _pump(qapp, 0.1)
    assert window.settings_page is None
    window.open_metadata_dialog()
    _pump(qapp, 0.3)
    window.set_language("zh-CN")
    _pump(qapp)
    assert window.metadata_page.tab_buttons[0].text() == "预设字段"
    window.metadata_page.close_page()


def test_status_pill_opens_settings(window, qapp):
    window.status_pill.click()
    _pump(qapp, 0.2)
    assert window.settings_page is not None
    window.settings_page.close_page()


def test_photo_mode_does_not_need_ffmpeg(window, qapp, monkeypatch):
    window.controller.detected_once = True
    window.controller.statuses = {}
    window.media_mode = "photo"
    assert window._tools_ready_for_mode()
    window.media_mode = "video"
    assert not window._tools_ready_for_mode()
    window._refresh_panel()
    assert window.start_button.mode == "setup"


def test_verification_report_with_skipped_and_failed_fields(window, monkeypatch, tmp_path):
    """Regression: skipped/mismatched metadata fields used to crash with a tr() argument clash."""
    from types import SimpleNamespace

    from uniqlab import app as app_module
    from uniqlab.media_metadata import VerifyReport

    report = VerifyReport(checked=3, passed=["Make"], failed=[("Model", "iPhone", "other")],
                          skipped=[("Software", "not supported")])
    monkeypatch.setattr(app_module, "verify_output", lambda *_args, **_kwargs: report)
    plan = SimpleNamespace(skipped=[("Software", "not supported")])
    summary = window._report_verification(tmp_path / "out.png", plan)
    assert "Model" in summary
    logged = []
    while not window.log_queue.empty():
        logged.append(window.log_queue.get_nowait())
    assert any("Software" in str(item[1]) for item in logged)
    assert any(item[0] == "warning" for item in logged)


def test_first_run_offer_shows_literal_ampersand(window, monkeypatch):
    """Native dialog buttons treat "&" as a mnemonic; the offer must show "Download & Install All"."""
    from PySide6.QtWidgets import QMessageBox

    seen = []

    def fake_exec(box):
        seen.extend(button.text() for button in box.buttons())
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(window.controller, "missing", lambda: ["ffmpeg"])
    monkeypatch.setattr(window.controller, "install", lambda *args, **kwargs: None)
    window.settings.set("first_run_offer_done", False)
    window._offer_install()
    assert "Download && Install All" in seen
    if window.settings_page is not None:
        window.settings_page.close_page()
