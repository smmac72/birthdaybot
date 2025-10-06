from __future__ import annotations

import logging
from typing import Tuple

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

from . import config

# repos
from .db.repo_users import UsersRepo
from .db.repo_groups import GroupsRepo
from .db.repo_friends import FriendsRepo

# handlers
from .handlers.start import StartHandler, AWAITING_REGISTRATION_BDAY
from .handlers.groups import GroupsHandler
from .handlers.friends import FriendsHandler
from .handlers.settings import SettingsHandler, S_WAIT_BDAY, S_WAIT_TZ, S_WAIT_ALERT
from .handlers.about import AboutHandler
from .handlers.birthdays import BirthdaysHandler  # <-- use dedicated handler

# keyboards
from .keyboards import main_menu_kb

# notif service
from .services.notif_service import NotifService


# logging setup
def _setup_logging() -> None:
    level = getattr(logging, (config.LOG_LEVEL or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


# build repos
def _build_repos() -> Tuple[UsersRepo, GroupsRepo, FriendsRepo]:
    db_path = config.DB_PATH
    users = UsersRepo(db_path)
    groups = GroupsRepo(db_path)
    friends = FriendsRepo(db_path)
    return users, groups, friends


# main menu
async def show_main_menu(update: Update, _):
    await update.message.reply_text("главное меню:", reply_markup=main_menu_kb())


# global error handler
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.getLogger("birthdaybot").exception("unhandled error", exc_info=context.error)


# test alerts command: /alert_test <hours>
async def alert_test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    notif: NotifService = app.bot_data.get("notif_service")  # type: ignore
    if not notif:
        await update.message.reply_text("notif service не готов.")
        return
    hours = 0
    if context.args:
        try:
            hours = int(context.args[0])
        except Exception:
            hours = 0
    hours = max(0, min(72, hours))
    person_id = update.effective_user.id
    sent = await notif.test_broadcast(person_id=person_id, hours=hours)
    await update.message.reply_text(f"тест: отправлено {sent} уведомл.")

async def who_follows_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notif: NotifService = context.application.bot_data.get("notif_service")  # type: ignore
    if not notif:
        await update.message.reply_text("notif service не готов.")
        return
    # оставляю как есть — предполагается, что метод есть в сервисе
    txt = await notif.debug_followers(update.effective_user.id) if hasattr(notif, "debug_followers") else "нет отладочного вывода."
    await update.message.reply_text(txt)


# build application and register handlers
def build_application() -> Application:
    _setup_logging()
    log = logging.getLogger("birthdaybot")

    users_repo, groups_repo, friends_repo = _build_repos()

    app = ApplicationBuilder().token(config.BOT_TOKEN).build()

    # stash repos
    app.bot_data["users_repo"] = users_repo
    app.bot_data["groups_repo"] = groups_repo
    app.bot_data["friends_repo"] = friends_repo

    # handler instances
    start_handler = StartHandler(users_repo)
    groups_handler = GroupsHandler(groups_repo, users_repo)
    friends_handler = FriendsHandler(users_repo, friends_repo, groups_repo)
    settings_handler = SettingsHandler(users_repo, friends_repo, groups_repo)
    about_handler = AboutHandler()
    birthdays_handler = BirthdaysHandler(users_repo, friends_repo, groups_repo)

    # errors
    app.add_error_handler(on_error)

    # start / registration
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("start", start_handler.start)],
            states={
                AWAITING_REGISTRATION_BDAY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, start_handler.reg_bday_entered)
                ]
            },
            fallbacks=[],
            name="conv_start_reg",
            persistent=False,
        ),
        group=0,
    )

    # birthdays screen (use proper handler)
    app.add_handler(MessageHandler(filters.Regex("^🎂 дни рождения$"), birthdays_handler.menu_entry), group=0)

    # groups flows
    for ch in groups_handler.conversation_handlers():
        app.add_handler(ch, group=0)
    app.add_handler(MessageHandler(filters.Regex("^👪 группы$"), groups_handler.menu_entry), group=0)

    # friends flows
    app.add_handler(MessageHandler(filters.Regex("^👥 управление друзьями$"), friends_handler.menu_entry), group=1)
    for ch in friends_handler.conversation_handlers():
        app.add_handler(ch, group=1)

    # settings
    app.add_handler(MessageHandler(filters.Regex("^⚙️ настройки$"), settings_handler.menu_entry), group=2)
    app.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^дата рождения$"), settings_handler.set_bday_start)],
            states={S_WAIT_BDAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_handler.set_bday_wait)]},
            fallbacks=[],
            name="conv_settings_bday",
            persistent=False,
        ),
        group=2,
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^часовой пояс$"), settings_handler.set_tz_start)],
            states={S_WAIT_TZ: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_handler.set_tz_wait)]},
            fallbacks=[],
            name="conv_settings_tz",
            persistent=False,
        ),
        group=2,
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^отложенность$"), settings_handler.set_alert_start)],
            states={S_WAIT_ALERT: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_handler.set_alert_wait)]},
            fallbacks=[],
            name="conv_settings_alert",
            persistent=False,
        ),
        group=2,
    )

    app.add_handler(MessageHandler(filters.Regex("^язык$"), settings_handler.change_lang), group=2)

    # about / donations
    app.add_handler(MessageHandler(filters.Regex("^ℹ️ о проекте$"), about_handler.menu_entry), group=3)
    app.add_handler(MessageHandler(filters.Regex(r"^\⭐ 50$"), about_handler.donate_50), group=3)
    app.add_handler(MessageHandler(filters.Regex(r"^\⭐ 100$"), about_handler.donate_100), group=3)
    app.add_handler(MessageHandler(filters.Regex(r"^\⭐ 500$"), about_handler.donate_500), group=3)
    app.add_handler(PreCheckoutQueryHandler(about_handler.precheckout), group=3)
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, about_handler.successful_payment), group=3)

    # exit to main
    app.add_handler(MessageHandler(filters.Regex("^⬅️ выйти$"), show_main_menu), group=3)
    app.add_handler(MessageHandler(filters.Regex("^◀️ вернуться в главное меню$"), show_main_menu), group=3)

    # test alerts
    app.add_handler(CommandHandler("alert_test", alert_test_cmd), group=3)
    app.add_handler(CommandHandler("who_follows", who_follows_cmd), group=3)

    # debug logger
    async def log_incoming(update: Update, _):
        if update.message and update.message.text:
            logging.getLogger("incoming").info("text=%r", update.message.text)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, log_incoming), group=99)

    # post-init: schedule window and daily refresh
    async def _post_init(application: Application):
        if getattr(application, "job_queue", None) is None:
            log.info("job queue not available, skipping schedule")
            return
        users = application.bot_data.get("users_repo")
        groups = application.bot_data.get("groups_repo")
        friends = application.bot_data.get("friends_repo")
        notif = NotifService(application, users, groups, friends)
        application.bot_data["notif_service"] = notif
        try:
            horizon = getattr(config, "SCHEDULE_HORIZON_DAYS", 7)
            await notif.schedule_all(horizon_days=horizon)
            await notif.schedule_daily_refresh(at_hour=3)
            log.info("birthday notifications scheduled and daily refresh set")
        except Exception as e:  # pragma: no cover
            log.exception("failed to schedule notifications: %s", e)

    app.post_init = _post_init

    return app


def main() -> None:
    app = build_application()
    app.run_polling()


if __name__ == "__main__":
    main()
