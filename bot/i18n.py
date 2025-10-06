from __future__ import annotations

import os
import re
import yaml
from typing import Dict, Any, Optional, Iterable

_LOCALES: Dict[str, Dict[str, str]] = {}
_LOCALES_DIR = os.path.join(os.path.dirname(__file__), "locales")  # expects en.yaml, ru.yaml, etc


# safe dict for format_map - leaves {missing} as-is
class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _load_locales() -> None:
    if _LOCALES:
        return
    if not os.path.isdir(_LOCALES_DIR):
        return
    for fname in os.listdir(_LOCALES_DIR):
        if not fname.endswith(".yaml"):
            continue
        code = os.path.splitext(fname)[0].lower()
        try:
            with open(os.path.join(_LOCALES_DIR, fname), "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                # flatten only str values, keep others out
                _LOCALES[code] = {str(k): str(v) for k, v in (data.items() if isinstance(data, dict) else [])}
        except Exception:
            # ignore broken locale to avoid startup crash
            pass


def available_languages() -> list[str]:
    _load_locales()
    # english first if present, then ru, then others
    pref = ["en", "ru"]
    rest = [c for c in _LOCALES.keys() if c not in pref]
    return [c for c in pref if c in _LOCALES] + sorted(rest)


def current_lang(*, update=None, context=None, default: str = "en") -> str:
    # context wins
    if context is not None:
        code = (context.user_data or {}).get("lang")
        if isinstance(code, str) and code in _LOCALES:
            return code
    # naive fallback by telegram locale
    if update is not None:
        try:
            user_lang = (getattr(update.effective_user, "language_code", None) or "").split("-")[0].lower()
            if user_lang in _LOCALES:
                return user_lang
        except Exception:
            pass
    # default
    return default if default in _LOCALES else (available_languages()[0] if available_languages() else "en")


def set_lang(code: str, *, context=None) -> None:
    if context is None:
        return
    _load_locales()
    if code in _LOCALES:
        context.user_data["lang"] = code


def t(key: str, *, update=None, context=None, **params) -> str:
    """translate key and safely format placeholders with params"""
    _load_locales()
    lang = current_lang(update=update, context=context)
    # try selected lang, then english, then raw key
    raw = (
        (_LOCALES.get(lang) or {}).get(key)
        or (_LOCALES.get("en") or {}).get(key)
        or key
    )
    try:
        return raw.format_map(_SafeDict(params))
    except Exception:
        # return raw to avoid crashing on client-side
        return raw


# buttons helpers

# language labels (TODO legacy, overriden by locales)
_DEFAULT_LANG_LABELS = {
    "en": "English 🇬🇧",
    "ru": "Русский 🇷🇺",
}

def language_label(code: str) -> str:
    _load_locales()
    # allow per-locale override: lang_label_<code> in that code or in en
    key = f"lang_label_{code}"
    for src in (code, "en"):
        val = (_LOCALES.get(src) or {}).get(key)
        if val:
            return val
    return _DEFAULT_LANG_LABELS.get(code, code)


def language_button_text(code: str) -> str:
    # single source for the keyboard caption
    return language_label(code)

def _strip_emoji_punct(s: str) -> str:
    # drop everything not letters/digits/space/hyphen
    return re.sub(r"[^\w\s\-]+", "", s, flags=re.UNICODE).strip()

def parse_language_choice(text: str) -> Optional[str]:
    s = (text or "").strip()
    for code in available_languages():
        if s == language_button_text(code):
            return code
    return None


def _escape_regex(s: str) -> str:
    return re.escape(s)


def btn_regex(key: str) -> str:
    """
    build a ^(?:opt1|opt2|...|optN)$ regex that matches this button across all locales.
    usage: MessageHandler(filters.Regex(btn_regex("btn_settings_lang")), handler)
    """
    _load_locales()
    texts: list[str] = []
    for code, mp in _LOCALES.items():
        val = mp.get(key)
        if isinstance(val, str) and val:
            texts.append(_escape_regex(val))
    if not texts:
        texts = [_escape_regex(key)]
    pattern = "^(?:" + "|".join(sorted(set(texts), key=lambda x: x.lower())) + ")$"
    return pattern
