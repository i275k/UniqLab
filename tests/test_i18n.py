"""Localization: catalog completeness, placeholders, plural forms, detection and live switching."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from uniqlab import i18n

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_PLURALS = {
    "en": {"one", "other"},
    "es": {"one", "other"},
    "pt-BR": {"one", "other"},
    "ru": {"one", "few", "many", "other"},
    "zh-CN": {"other"},
}


def _forms(value) -> list[str]:
    return list(value.values()) if isinstance(value, dict) else [value]


def _placeholders(value) -> set[str]:
    found = set()
    for form in _forms(value):
        found |= i18n.placeholders(form)
    return found


def test_supported_locales_exactly():
    assert i18n.SUPPORTED_LOCALES == ("en", "zh-CN", "pt-BR", "es", "ru")
    for locale in i18n.SUPPORTED_LOCALES:
        assert (ROOT / "uniqlab/i18n/locales" / f"{locale}.json").exists()


@pytest.mark.parametrize("locale", i18n.SUPPORTED_LOCALES)
def test_catalog_has_every_key(locale):
    reference = i18n.catalog("en")
    catalog = i18n.catalog(locale)
    assert set(catalog) == set(reference), sorted(set(catalog) ^ set(reference))[:20]


@pytest.mark.parametrize("locale", i18n.SUPPORTED_LOCALES)
def test_values_are_non_empty_strings(locale):
    for key, value in i18n.catalog(locale).items():
        for form in _forms(value):
            assert isinstance(form, str) and form.strip(), f"{locale}:{key}"


@pytest.mark.parametrize("locale", i18n.SUPPORTED_LOCALES)
def test_placeholders_match_english(locale):
    reference = i18n.catalog("en")
    for key, value in i18n.catalog(locale).items():
        expected = _placeholders(reference[key])
        if isinstance(reference[key], dict):
            # A singular form may omit {n} ("The video is saved"), but never invent names.
            for form in _forms(value):
                assert i18n.placeholders(form) <= expected, f"{locale}:{key}"
        else:
            assert i18n.placeholders(value) == expected, f"{locale}:{key}"


@pytest.mark.parametrize("locale", i18n.SUPPORTED_LOCALES)
def test_plural_forms_complete(locale):
    reference = i18n.catalog("en")
    for key, value in i18n.catalog(locale).items():
        if isinstance(reference[key], dict):
            assert isinstance(value, dict), f"{locale}:{key} must be a plural object"
            assert REQUIRED_PLURALS[locale] <= set(value), f"{locale}:{key}"


@pytest.mark.parametrize(("n", "category"), [(0, "many"), (1, "one"), (2, "few"), (4, "few"), (5, "many"),
                                             (11, "many"), (12, "many"), (21, "one"), (22, "few"), (111, "many")])
def test_russian_plural_rules(n, category):
    assert i18n.plural_category("ru", n) == category


def test_other_plural_rules():
    assert i18n.plural_category("en", 1) == "one" and i18n.plural_category("en", 0) == "other"
    assert i18n.plural_category("es", 1) == "one" and i18n.plural_category("es", 2) == "other"
    assert i18n.plural_category("pt-BR", 0) == "one" and i18n.plural_category("pt-BR", 2) == "other"
    assert i18n.plural_category("zh-CN", 1) == "other"


@pytest.mark.parametrize(("tags", "expected"), [
    (["zh-Hans-CN"], "zh-CN"), (["zh-TW"], "zh-CN"), (["pt-PT"], "pt-BR"), (["pt_BR"], "pt-BR"),
    (["es-419"], "es"), (["ru-RU"], "ru"), (["en-GB"], "en"), (["de-DE"], "en"), ([], "en"),
    (["fr-FR", "es-ES"], "es"),
])
def test_resolve_locale(tags, expected):
    assert i18n.resolve_locale(tags) == expected


def test_tr_fallback_and_formatting(english):
    assert i18n.tr("no.such.key") == "no.such.key"
    assert i18n.tr("files.count.video", n=1) == "1 video"
    assert i18n.tr("files.count.video", n=3) == "3 videos"
    i18n.set_locale("ru")
    assert i18n.tr("files.origin.folders", n=5) == "из 5 папок"
    assert i18n.tr("files.origin.folders", n=1) == "из 1 папки"
    assert i18n.tr("files.origin.folders", n=2) == "из 2 папок"
    # Missing parameters never raise.
    assert "{name}" in i18n.tr("files.origin.folder")


def test_listeners_called_on_switch(english):
    seen = []
    i18n.add_listener(seen.append)
    i18n.set_locale("es")
    i18n.set_locale("es")  # no duplicate notification
    assert seen == ["es"]


def test_unknown_locale_falls_back(english):
    i18n.set_locale("fr")
    assert i18n.current_locale() == "en"


def test_decimal_separator(english):
    assert i18n.decimal(1.25, 1) in ("1.2", "1.3")
    i18n.set_locale("ru")
    assert "," in i18n.decimal(1.5)


def test_every_literal_key_in_code_exists():
    """Every tr("…") literal (and other literal i18n keys) used by the code is in the catalog."""
    pattern = re.compile(r'''\btr\(\s*["']([a-z][a-z0-9_]*(?:\.[A-Za-z0-9_\-]+)+)["']''')
    missing = []
    for path in (ROOT / "uniqlab").rglob("*.py"):
        for key in pattern.findall(path.read_text(encoding="utf-8")):
            if not i18n.has_key(key):
                missing.append(f"{path.name}: {key}")
    assert not missing, missing


def test_no_cyrillic_left_in_code():
    """User-facing text lives in the catalogs; source code stays language-neutral."""
    offenders = []
    for path in (ROOT / "uniqlab").rglob("*.py"):
        if path.name == "__init__.py" and path.parent.name == "i18n":
            continue  # holds the native language names
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search("[А-Яа-яЁё]", line):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, offenders


def test_brand_and_credit_stay_untranslated():
    from uniqlab.app import BrandWordmark, CreditLink

    assert BrandWordmark.CAPTION == "VIDEO & IMAGE PROCESSING"
    assert BrandWordmark.NAME == "UNIQLAB"
    assert CreditLink.TEXT == "by onyx.x (i275k)"


def test_parameter_named_key_does_not_clash(english):
    """Catalog placeholders may be called {key}; tr() must accept them as normal parameters."""
    assert "Make" in i18n.tr("log.meta_skipped", key="Make", reason="not supported")
    assert "Model" in i18n.tr("meta.verify.mismatch_item", key="Model", expected="a", actual="b")
