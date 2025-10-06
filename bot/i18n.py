from __future__ import annotations

import os
import glob
import logging
from typing import Any, Dict, Optional

try:
    import yaml  # pyyaml
except ImportError:
    yaml = None  # type: ignore

log = logging.getLogger("i18n")

# locales live here: bot/locales/{code}.yaml
_LOCALES_DIR = os.path.join(os.path.dirname(__file__), "locales")

# cache: {lang_code: {key: value}}
_CACHE: Dict[str, Dict[str, Any]] = {}

# defaults
_DEFAULT_LANG = "ru"
_FALLBACK_LANG = "en"

# native labels (used if yaml has no lang_label)
_LANG_LABELS = {
    "ru": "Русский",
    "en": "English",
}

# flags for pretty language buttons
_LANG_FLAGS = {
    "ru": "🇷🇺",
    "en": "🇬🇧",
}

# ---------- loading ----------

def _load_lang(code: str) -> Dict[str, Any]:
    code = (code or "").lower()
    if code in _CACHE:
        return _CACHE[code]

    path = os.path.join(_LOCALES_DIR, f"{code}.yaml")
    data: Dict[str, Any] = {}

    if yaml and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
                if isinstance(loaded, dict):
                    data = loaded
        except Exception as e:
            log.exception("failed to load locale %s: %s", code, e)

    _CACHE[code] = data
    return data

def available_languages() -> list[str]:
    codes = []
    try:
        for p in glob.glob(os.path.join(_LOCALES_DIR, "*.yaml")):
            base = os.path.basename(p)
            c = base[:-5] if base.endswith(".yaml") else base
            if c:
                codes.append(c.lower())
    except Exception as e:
        log.exception("glob locales failed: %s", e)

    codes = sorted(set(codes))
    if codes:
        ordered = []
        for head in ("ru", "en"):
            if head in codes:
                ordered.append(head)
        for c in codes:
            if c not in ordered:
                ordered.append(c)
        return ordered

    fallback = [c for c in ("ru", "en") if c in _LANG_LABELS]
    return fallback or ["ru"]

# ---------- language resolution ----------

def _norm_lang(code: Optional[str]) -> str:
    if not code:
        return _DEFAULT_LANG
    c = code.lower()
    if "-" in c:
        c = c.split("-", 1)[0]
    langs = set(available_languages())
    if c in langs:
        return c
    if _FALLBACK_LANG in langs:
        return _FALLBACK_LANG
    return _DEFAULT_LANG

def current_lang(*, update=None, context=None, explicit: Optional[str] = None) -> str:
    if explicit:
        return _norm_lang(explicit)

    if context is not None:
        u = getattr(context, "user_data", None)
        if isinstance(u, dict) and u.get("lang"):
            return _norm_lang(u["lang"])
        c = getattr(context, "chat_data", None)
        if isinstance(c, dict) and c.get("lang"):
            return _norm_lang(c["lang"])

    try:
        if update and getattr(update, "effective_user", None):
            lc = (update.effective_user.language_code or "").strip()
            if lc:
                return _norm_lang(lc)
    except Exception:
        pass

    return _DEFAULT_LANG

def set_lang(code: str, *, context=None) -> str:
    lang = _norm_lang(code)
    if context is not None:
        if hasattr(context, "user_data") and isinstance(context.user_data, dict):
            context.user_data["lang"] = lang
        if hasattr(context, "chat_data") and isinstance(context.chat_data, dict):
            context.chat_data["lang"] = lang
    return lang

def language_label(code: Optional[str], *, update=None, context=None) -> str:
    c = _norm_lang(code or current_lang(update=update, context=context))
    data = _load_lang(c)
    label = data.get("lang_label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    return _LANG_LABELS.get(c, c)

def language_flag(code: Optional[str]) -> str:
    c = _norm_lang(code)
    return _LANG_FLAGS.get(c, "")

def language_button_text(code: str) -> str:
    # flag + native label for picker buttons
    return f"{language_flag(code)} {language_label(code)}".strip()

def parse_language_choice(text: str) -> Optional[str]:
    # accept either "English", "Русский" or "🇬🇧 English" etc.
    s = (text or "").strip().lower()
    if not s:
        return None
    for c in available_languages():
        lbl = language_label(c).lower()
        flg = language_flag(c)
        if s == lbl or s == f"{flg} {lbl}".lower() or s == flg.lower():
            return c
    return None

# ---------- translation ----------

def t(key: str, *, update=None, context=None, lang: Optional[str] = None, **params) -> str:
    code = _norm_lang(lang or current_lang(update=update, context=context))
    val = _load_lang(code).get(key)

    if val is None and _FALLBACK_LANG and _FALLBACK_LANG != code:
        val = _load_lang(_FALLBACK_LANG).get(key)
    if val is None and _DEFAULT_LANG not in (code, _FALLBACK_LANG):
        val = _load_lang(_DEFAULT_LANG).get(key)
    if val is None:
        val = key

    if isinstance(val, str):
        try:
            return val.format(**params)
        except Exception:
            return val
    try:
        return str(val)
    except Exception:
        return key

# ---------- helpers for button regex ----------

def btn_regex(key: str) -> str:
    """
    build a regex that matches this button text in all languages
    """
    import re
    variants = []
    for code in available_languages():
        v = _load_lang(code).get(key)
        if isinstance(v, str) and v.strip():
            variants.append(re.escape(v.strip()))
    if not variants:
        variants.append(re.escape(key))
    return "^(" + "|".join(variants) + ")$"
