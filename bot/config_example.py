import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "INSERT_HERE")

DB_PATH = os.getenv("DB_PATH", "birthday_bot.db")

SCHEDULE_HORIZON_DAYS = 370

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

DEFAULT_LANG = os.getenv("DEFAULT_LANG", "ru")

LOCALE_PATH = os.getenv("LOCALE_PATH", "locale")

# admin stuff
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "INSERT_HERE")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)
def _parse_ids(s: str):
    out = []
    for x in (s or "").split(","):
        x = x.strip()
        if x.isdigit():
            out.append(int(x))
    return out
ADMIN_ALLOWED_IDS = _parse_ids(os.getenv("ADMIN_ALLOWED_IDS", "INSERT_ADMIN_IDS_HERE"))