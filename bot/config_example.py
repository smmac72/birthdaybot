import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "INSERT_HERE")

DB_PATH = os.getenv("DB_PATH", "birthday_bot.db")

SCHEDULE_HORIZON_DAYS = 370

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")