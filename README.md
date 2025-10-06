
# BirthdayBot

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/) [![Docker Ready](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![CI/CD](https://github.com/smmac72/birthdaybot/actions/workflows/docker.yml/badge.svg)](https://github.com/smmac72/birthdaybot/actions) [![CI/CD](https://github.com/smmac72/birthdaybot/actions/workflows/deploy.yml/badge.svg)](https://github.com/smmac72/birthdaybot/actions) 

An open-source **Telegram bot** that reminds you about birthdays of friends and group members — with timezones, alerts, wishlists, and maintenance mode.

## Features
- Personal and group birthday tracking
- Early alerts
- Automatic rescheduling on date/timezone change
- Multi-language UI (RU/EN - I'm cool with you contributing more)
- Followers via friends or shared groups
- Individual wishlists
- Admin & maintenance tools
- Docker-ready deployment with GitHub Actions CI/CD

## Project structure
```
bot/
├── handlers/ # Telegram handlers (start, settings, friends, groups, birthdays, about)
├── services/ # Notification & maintenance schedulers
├── db/ # AsyncSQLite repos (users, groups, friends)
├── locales/ # Translations (.yaml)
├── keyboards.py # Menu layouts
├── i18n.py # i18n helpers
├── config.py # Environment configuration (env-driven)
└── main.py # Entry point

```
## Contribution
[CONTRIBUTING RULES](CONTRIBUTING.md)

## Running locally
1.  **Install dependencies**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
2.  **Create a `.env` file**
```bash
# === EXAMPLE .ENV FILE (use .env-prod or .env-stg for local tests) ===
BOT_TOKEN=INSERTHERE
ADMIN_BOT_TOKEN=INSERTHERE
ADMIN_CHAT_ID=0
ADMIN_ALLOWED_IDS=YOURID
DB_PATH=/app/data/birthday_bot.db
LOG_LEVEL=INFO
DEFAULT_LANG=ru
LOCALE_PATH=bot/locales
SCHEDULE_HORIZON_DAYS=370
TZ=Europe/Moscow
```
3.  **Run the bot**
```bash
python -m bot.main
```
---
## Docker deployment
The repository ships with a ready `docker-compose.yml` that launches:
-  `birthdaybot-prod` — main bot
-  `adminbot-prod` — admin service
To build and run:
```bash
docker  compose  --env-file  .env.prod  up  -d  birthdaybot-prod  adminbot-prod
```
> Example `examle.env` is provided.
---
## GitHub Actions (CI/CD)
The repository includes an automatic deployment workflow in
`.github/workflows/deploy.yml`
- pushes to `main` → deploy to PROD
- pushes to `stage` → deploy to STAGE

## Tech stack
-  **Python 3.11+**
-  **python-telegram-bot 21.x (async)**
-  **aiosqlite**
-  **Docker + Compose**
-  **GitHub Actions** for CI/CD

## Maintenance mode
Admins can toggle maintenance mode via database table `admin_state`.
Does it work? lmao no
Modes:
-  `off:soft` — normal operation
-  `on:soft` — menus disabled; reminders paused
-  `on:hard` — bot fully stops

## License
MIT © 2025 [smmac72](https://github.com/smmac72)

## Support
If you enjoy the bot — consider starring the repo ⭐
or donating via Telegram Stars in the About menu!