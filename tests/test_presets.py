"""Metadata presets: translations exist for every field/preset text and built-ins are valid."""

from __future__ import annotations

import pytest

from uniqlab import i18n
from uniqlab.metadata_presets import (
    BUILTIN_PROFILES,
    FIELDS,
    ProfileStore,
    field_spec,
    profile_group,
    profile_name,
    profile_summary,
    validate_value,
)


def test_field_texts_exist():
    for spec in FIELDS:
        assert i18n.has_key(f"{spec.i18n}.label"), spec.key
        assert i18n.has_key(f"fgroup.{spec.group}"), spec.group
        if spec.has_note:
            assert i18n.has_key(f"{spec.i18n}.note"), spec.key
        for option in spec.options:
            assert i18n.has_key(f"{spec.i18n}.options.{str(option).strip() or 'blank'}"), (spec.key, option)


def test_builtin_profile_texts_exist():
    for profile in BUILTIN_PROFILES:
        for key in (profile["name_key"], profile["summary_key"], profile["group"]):
            assert i18n.has_key(key), (profile["id"], key)
        for entry in profile.get("evidence", []):
            assert i18n.has_key(entry["key"] + ".title"), entry
            assert i18n.has_key(entry["key"] + ".detail"), entry
            if "url" in entry:
                assert entry["url"].startswith("https://"), entry
        for key in profile.get("limitations", []):
            assert i18n.has_key(key), key


def test_builtin_values_validate():
    for profile in BUILTIN_PROFILES:
        for key, value in profile.get("fields", {}).items():
            spec = field_spec(profile["media"], key)
            assert spec is not None, (profile["id"], key)
            assert validate_value(spec, value) is None, (profile["id"], key, value)


def test_presets_never_write_location():
    for profile in BUILTIN_PROFILES:
        for key in profile.get("fields", {}):
            assert "GPS" not in key and "location" not in key.lower(), (profile["id"], key)


@pytest.mark.parametrize("locale", i18n.SUPPORTED_LOCALES)
def test_profile_helpers_translate(locale):
    previous = i18n.current_locale()
    i18n.set_locale(locale)
    try:
        for profile in BUILTIN_PROFILES:
            assert profile_name(profile) and not profile_name(profile).startswith("preset.")
            assert profile_summary(profile) and not profile_summary(profile).startswith("preset.")
            assert not profile_group(profile).startswith("group.")
    finally:
        i18n.set_locale(previous)


def test_validation_messages_are_localized(english):
    spec = field_spec("photo", "ExifIFD:ExposureProgram")
    assert validate_value(spec, "9").startswith("allowed:")
    i18n.set_locale("es")
    assert validate_value(spec, "9").startswith("permitido:")


def test_save_as_keeps_language_neutral_keys(tmp_path, english):
    store = ProfileStore(tmp_path / "profiles.json")
    source = store.get("builtin:photo-iphone-15-pro")
    custom = store.save_as(source, "My iPhone")
    assert custom["name"] == "My iPhone" and "name_key" not in custom
    assert custom["group"] == "group.custom"
    assert custom["based_on_key"] == source["name_key"]
    assert profile_group(custom) == "My profiles"
    i18n.set_locale("ru")
    assert profile_group(custom) == "Мои профили"
    reloaded = ProfileStore(tmp_path / "profiles.json")
    assert reloaded.get(custom["id"])["name"] == "My iPhone"


def test_modified_builtin_can_be_reset(tmp_path):
    store = ProfileStore(tmp_path / "profiles.json")
    profile = store.get("builtin:photo-iphone-15-pro")
    profile["fields"]["IFD0:Artist"] = "Someone"
    store.save(profile)
    assert store.get(profile["id"])["modified"]
    store.reset(profile["id"])
    assert not store.is_modified(profile["id"])
