# bot/config.py
import os
from pathlib import Path
from typing import List

def _env_str(key: str, default: str = "") -> str:
    return os.getenv(key, default)

def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(os.getenv(key, str(default)).strip())
    except Exception:
        return default

def _parse_ids(s: str) -> List[int]:
    out: List[int] = []
    for x in (s or "").split(","):
        x = x.strip()
        if x and x.lstrip("-").isdigit():
            try:
                out.append(int(x))
            except Exception:
                pass
    return out

# --- main bot tokens / ids ---
BOT_TOKEN = _env_str("BOT_TOKEN", "INSERT_HERE")

# admin stuff
ADMIN_BOT_TOKEN = _env_str("ADMIN_BOT_TOKEN", "INSERT_HERE")
ADMIN_CHAT_ID = _env_int("ADMIN_CHAT_ID", 0)
ADMIN_ALLOWED_IDS = _parse_ids(_env_str("ADMIN_ALLOWED_IDS", ""))

# --- storage / paths ---
DB_PATH = _env_str("DB_PATH", "/app/data/birthday_bot.db")
LOCALE_PATH = _env_str("LOCALE_PATH", "bot/locales")

try:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

# --- behavior ---
SCHEDULE_HORIZON_DAYS = _env_int("SCHEDULE_HORIZON_DAYS", 370)
LOG_LEVEL = _env_str("LOG_LEVEL", "INFO")
DEFAULT_LANG = _env_str("DEFAULT_LANG", "ru")
DEFAULT_TZ = _env_str("DEFAULT_TZ", "UTC")

SELF_BDAY_HOUR = _env_int("SELF_BDAY_HOUR", 9)
SELF_BDAY_MINUTE = _env_int("SELF_BDAY_MINUTE", 0)
