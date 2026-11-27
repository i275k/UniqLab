"""Localization: JSON catalogs, plural rules and system-language detection.

Catalogs live in ``uniqlab/i18n/locales/<locale>.json`` as nested objects; keys are the
dotted paths. English is the reference catalog and the fallback for any missing key.
A plural entry is an object of CLDR categories ({"one": …, "few": …, "many": …,
"other": …}) selected by the ``n`` parameter.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

SUPPORTED_LOCALES = ("en", "zh-CN", "pt-BR", "es", "ru")
NATIVE_NAMES = {
    "en": "English",
    "zh-CN": "简体中文",
    "pt-BR": "Português (Brasil)",
    "es": "Español",
    "ru": "Русский",
}
SHORT_NAMES = {"en": "EN", "zh-CN": "中文", "pt-BR": "PT", "es": "ES", "ru": "RU"}
DEFAULT_LOCALE = "en"

_LOCALES_DIR = Path(__file__).resolve().parent / "locales"
_catalogs: dict[str, dict] = {}
_current = DEFAULT_LOCALE
_listeners: list[Callable[[str], None]] = []


def _flatten(data: dict, prefix: str = "") -> dict:
    flat = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and not _is_plural(value):
            flat.update(_flatten(value, path + "."))
        else:
            flat[path] = value
    return flat


def _is_plural(value: dict) -> bool:
    return bool(value) and set(value) <= {"zero", "one", "two", "few", "many", "other"} and "other" in value


def catalog(locale: str) -> dict:
    if locale not in _catalogs:
        path = _LOCALES_DIR / f"{locale}.json"
        _catalogs[locale] = _flatten(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else {}
    return _catalogs[locale]


def plural_category(locale: str, n) -> str:
    """CLDR cardinal rules for the supported languages (integers)."""
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        return "other"
    if locale == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    if locale == "pt-BR":
        return "one" if n in (0, 1) else "other"
    if locale == "zh-CN":
        return "other"
    return "one" if n == 1 else "other"  # en, es


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def tr(key: str, /, **params) -> str:
    value = catalog(_current).get(key)
    locale = _current
    if value is None:
        value = catalog(DEFAULT_LOCALE).get(key)
        locale = DEFAULT_LOCALE
    if value is None:
        return key
    if isinstance(value, dict):
        value = value.get(plural_category(locale, params.get("n", 0))) or value.get("other", key)
    try:
        return str(value).format_map(_SafeDict(params))
    except (ValueError, IndexError):
        return str(value)


def has_key(key: str) -> bool:
    return key in catalog(DEFAULT_LOCALE)


def current_locale() -> str:
    return _current


def set_locale(locale: str) -> None:
    global _current
    locale = locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE
    if locale == _current:
        return
    _current = locale
    for listener in list(_listeners):
        listener(locale)


def add_listener(listener: Callable[[str], None]) -> None:
    _listeners.append(listener)


def resolve_locale(tags) -> str:
    """Map OS language tags (e.g. "zh-Hans-CN", "pt-PT", "es-419") to a supported locale."""
    for raw in tags or ():
        tag = str(raw).replace("_", "-").strip()
        lower = tag.lower()
        language = lower.split("-")[0]
        if language == "zh":
            # Traditional Chinese has no catalog; Simplified is the closest supported script.
            return "zh-CN"
        if language == "pt":
            return "pt-BR"
        if language in ("es", "ru", "en"):
            return language
    return DEFAULT_LOCALE


def system_locale() -> str:
    try:
        from PySide6.QtCore import QLocale

        return resolve_locale(QLocale.system().uiLanguages())
    except Exception:
        return DEFAULT_LOCALE


def decimal(value: float, digits: int = 1) -> str:
    text = f"{value:.{digits}f}"
    return text.replace(".", ",") if _current in ("ru", "pt-BR", "es") else text


PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text))
