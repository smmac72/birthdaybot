from __future__ import annotations

# main entry with maintenance guard and admin events polling

import asyncio
import logging
from typing import Tuple, List

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
from .db.repo_wishlist import WishlistRepo  # <-- NEW

# handlers
from .handlers.start import StartHandler, AWAITING_LANG_PICK, AWAITING_REGISTRATION_BDAY
from .handlers.groups import GroupsHandler
from .handlers.friends import FriendsHandler
from .handlers.settings import SettingsHandler, S_WAIT_BDAY, S_WAIT_TZ, S_WAIT_ALERT_DAYS, S_WAIT_ALERT_TIME, S_WAIT_LANG
from .handlers.about import AboutHandler
from .handlers.wishlist import WishlistHandler  # <-- NEW

# keyboards
from .keyboards import main_menu_kb

# notif service
from .services.notif_service import NotifService

# i18n
from .i18n import t, btn_regex

# re-use admin repo to read events and chat list
from .adminbot.repo import AdminRepo


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
def _build_repos() -> Tuple[UsersRepo, GroupsRepo, FriendsRepo, WishlistRepo]:
    db_path = config.DB_PATH
    users = UsersRepo(db_path)
    groups = GroupsRepo(db_path)
    friends = FriendsRepo(db_path)
    wishlist = WishlistRepo(db_path)  # <-- NEW
    return users, groups, friends, wishlist


# helpers

def _is_admin(update: Update) -> bool:
    try:
        uid = update.effective_user.id
    except Exception:
        return False
    allowed = getattr(config, "ADMIN_ALLOWED_IDS", []) or []
    return bool(uid and uid in allowed)

async def _broadcast_key_to_all(app: Application, users_repo: UsersRepo, key: str) -> int:
    # per-user localization by reading profile lang
    repo = AdminRepo(config.DB_PATH)
    chat_ids = await repo.list_all_chat_ids()
    sent = 0
    for cid in chat_ids:
        lang = "en"
        try:
            u = await users_repo.get_user(int(cid))
            if u and u.get("lang"):
                lang = str(u["lang"])
        except Exception:
            pass
        text = t(key) if callable(t) else key
        try:
            await app.bot.send_message(chat_id=cid, text=text)
            sent += 1
        except Exception:
            pass
    return sent


# main menu
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(t("main_menu_title"), reply_markup=main_menu_kb(context=context))


# global error handler
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.getLogger("birthdaybot").exception("unhandled error", exc_info=context.error)


# test alerts command: /alert_test <hours>
async def alert_test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    notif: NotifService = app.bot_data.get("notif_service")  # type: ignore
    if not notif:
        await update.message.reply_text("notif service not ready.")
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
    await update.message.reply_text(f"test: sent {sent}.")


# maintenance guard (soft): block any user input except admins
async def maintenance_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    maint = context.application.bot_data.get("maintenance") or {}
    if not maint.get("enabled"):
        return  # pass through

    # admins bypass
    if _is_admin(update):
        return

    # hard: if still alive, behave like soft
    mode = maint.get("mode", "soft")
    key = maint.get("key") or ("maintenance_hard" if mode == "hard" else "maintenance_soft")

    cd = context.chat_data
    mem_key = f"maint_notified:{mode}"
    if not cd.get(mem_key):
        try:
            await (update.effective_message or update.message).reply_text(t(key))
        except Exception:
            pass
        cd[mem_key] = True

    raise asyncio.CancelledError  # abort rest of handlers


# --- admin events polling (from admin_events table)
async def _process_admin_events(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    users_repo: UsersRepo = app.bot_data["users_repo"]
    notif: NotifService = app.bot_data.get("notif_service")
    repo = AdminRepo(config.DB_PATH)

    try:
        events = await repo.fetch_pending_events(limit=50)
    except Exception as e:
        logging.getLogger("birthdaybot").exception("fetch admin events failed: %s", e)
        return

    if not events:
        return

    done_ids: List[int] = []
    for ev in events:
        kind = ev.get("kind")
        payload = ev.get("payload") or {}
        if kind == "maint":
            key = payload.get("key") or "maintenance_soft"
            if key == "maintenance_on_soft":
                app.bot_data["maintenance"] = {"enabled": True, "mode": "soft", "key": "maintenance_soft"}
                await _broadcast_key_to_all(app, users_repo, "maintenance_soft")
            elif key == "maintenance_on_hard":
                app.bot_data["maintenance"] = {"enabled": True, "mode": "hard", "key": "maintenance_hard"}
                await _broadcast_key_to_all(app, users_repo, "maintenance_hard")
                try:
                    if notif:
                        await notif.shutdown()
                except Exception:
                    pass
                async def _stop():
                    await asyncio.sleep(1.0)
                    try:
                        await app.stop()
                    except Exception:
                        pass
                asyncio.create_task(_stop())
            elif key == "maintenance_off":
                app.bot_data["maintenance"] = {"enabled": False, "mode": "soft", "key": None}
                await _broadcast_key_to_all(app, users_repo, "maintenance_off")
            done_ids.append(int(ev["id"]))
        else:
            done_ids.append(int(ev["id"]))

    try:
        if done_ids:
            await repo.mark_events_processed(done_ids)
    except Exception:
        pass


# build application and register handlers
def build_application() -> Application:
    _setup_logging()
    log = logging.getLogger("birthdaybot")

    users_repo, groups_repo, friends_repo, wishlist_repo = _build_repos()

    app = ApplicationBuilder().token(config.BOT_TOKEN).build()

    app.bot_data["users_repo"] = users_repo
    app.bot_data["groups_repo"] = groups_repo
    app.bot_data["friends_repo"] = friends_repo
    app.bot_data["wishlist_repo"] = wishlist_repo  # <-- NEW
    app.bot_data.setdefault("maintenance", {"enabled": False, "mode": "soft", "key": None})

    start_handler = StartHandler(users_repo)
    groups_handler = GroupsHandler(groups_repo, users_repo)
    friends_handler = FriendsHandler(users_repo, friends_repo, groups_repo)
    settings_handler = SettingsHandler(users_repo, friends_repo, groups_repo)
    about_handler = AboutHandler()
    wishlist_handler = WishlistHandler(wishlist_repo, users_repo)  # <-- NEW

    app.add_error_handler(on_error)

    # maintenance guard first
    app.add_handler(MessageHandler(filters.ALL, maintenance_guard), group=-100)

    # start / registration
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("start", start_handler.start)],
            states={
                AWAITING_LANG_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, start_handler.lang_pick_entered)],
                AWAITING_REGISTRATION_BDAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, start_handler.reg_bday_entered)],
            },
            fallbacks=[],
            name="conv_start_reg",
            persistent=False,
        ),
        group=0,
    )

    # birthdays screen
    async def show_birthdays(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from .handlers.birthdays import BirthdaysHandler
        bh = context.application.bot_data.get("birthdays_handler")
        if not bh:
            bh = BirthdaysHandler(users_repo, friends_repo, groups_repo)
            context.application.bot_data["birthdays_handler"] = bh
        await bh.menu_entry(update, context)

    app.add_handler(MessageHandler(filters.Regex(btn_regex("btn_birthdays")), show_birthdays), group=0)

    # wishlist wiring (nested under birthdays)
    # direct actions
    app.add_handler(MessageHandler(filters.Regex(btn_regex("btn_wishlist_my")), wishlist_handler.my_list), group=0)
    # conversations
    for ch in wishlist_handler.conversation_handlers():
        app.add_handler(ch, group=0)
    # back to birthdays from nested menu
    app.add_handler(MessageHandler(filters.Regex(btn_regex("btn_back")), show_birthdays), group=0)

    # groups flows
    for ch in groups_handler.conversation_handlers():
        app.add_handler(ch, group=0)
    app.add_handler(MessageHandler(filters.Regex(btn_regex("btn_groups")), groups_handler.menu_entry), group=0)

    # friends flows
    app.add_handler(MessageHandler(filters.Regex(btn_regex("btn_friends")), friends_handler.menu_entry), group=1)
    for ch in friends_handler.conversation_handlers():
        app.add_handler(ch, group=1)

    # settings
    app.add_handler(MessageHandler(filters.Regex(btn_regex("btn_settings")), settings_handler.menu_entry), group=2)

    app.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.Regex(btn_regex("btn_settings_bday")), settings_handler.set_bday_start)],
            states={S_WAIT_BDAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_handler.set_bday_wait)]},
            fallbacks=[],
            name="conv_settings_bday",
            persistent=False,
        ),
        group=2,
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.Regex(btn_regex("btn_settings_tz")), settings_handler.set_tz_start)],
            states={S_WAIT_TZ: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_handler.set_tz_wait)]},
            fallbacks=[],
            name="conv_settings_tz",
            persistent=False,
        ),
        group=2,
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.Regex(btn_regex("btn_settings_alert")), settings_handler.set_alert_start)],
            states={
                S_WAIT_ALERT_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_handler.set_alert_wait_days)],
                S_WAIT_ALERT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_handler.set_alert_wait_time)],
            },
            fallbacks=[],
            name="conv_settings_alert",
            persistent=False,
        ),
        group=2,
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.Regex(btn_regex("btn_settings_lang")), settings_handler.change_lang_start)],
            states={S_WAIT_LANG: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_handler.change_lang_wait)]},
            fallbacks=[MessageHandler(filters.Regex(btn_regex("btn_cancel")), settings_handler.menu_entry)],
            name="conv_settings_lang",
            persistent=False,
        ),
        group=2,
    )

    # about / donations
    app.add_handler(MessageHandler(filters.Regex(btn_regex("btn_about")), about_handler.menu_entry), group=3)
    app.add_handler(MessageHandler(filters.Regex(r"^\⭐ 50$"), about_handler.donate_50), group=3)
    app.add_handler(MessageHandler(filters.Regex(r"^\⭐ 100$"), about_handler.donate_100), group=3)
    app.add_handler(MessageHandler(filters.Regex(r"^\⭐ 500$"), about_handler.donate_500), group=3)
    app.add_handler(PreCheckoutQueryHandler(about_handler.precheckout), group=3)
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, about_handler.successful_payment), group=3)

    # exit/back to main
    app.add_handler(MessageHandler(filters.Regex(btn_regex("btn_exit")), show_main_menu), group=3)
    app.add_handler(MessageHandler(filters.Regex(btn_regex("btn_back_main")), show_main_menu), group=3)

    # test alerts
    app.add_handler(CommandHandler("alert_test", alert_test_cmd), group=3)

    # debug logger last
    async def log_incoming(update: Update, _):
        if update.message and update.message.text:
            logging.getLogger("incoming").info("text=%r", update.message.text)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, log_incoming), group=99)

    # post-init: schedule window, daily refresh, admin events poller
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
            application.job_queue.run_repeating(_process_admin_events, interval=5.0, first=3.0, name="admin_events_poll")
            log.info("birthday notifications scheduled, daily refresh set, admin events poller on")
        except Exception as e:
            log.exception("post-init failed: %s", e)

    app.post_init = _post_init

    return app


def main() -> None:
    app = build_application()
    app.run_polling()


if __name__ == "__main__":
    main()
