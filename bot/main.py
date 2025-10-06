from __future__ import annotations

import logging
from typing import Dict, Tuple, Optional

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
from .handlers.start import StartHandler, AWAITING_REGISTRATION_BDAY, AWAITING_LANG_PICK
from .handlers.groups import GroupsHandler
from .handlers.friends import FriendsHandler
from .handlers.settings import SettingsHandler, S_WAIT_BDAY, S_WAIT_TZ, S_WAIT_ALERT, S_WAIT_LANG
from .handlers.about import AboutHandler

# keyboards
from .keyboards import main_menu_kb

# i18n
from .i18n import t, btn_regex

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
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(t("main_menu_title", update=update, context=context), reply_markup=main_menu_kb(update=update, context=context))


# birthdays overview — now i18n
async def show_birthdays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log = logging.getLogger("birthdays")
    users: UsersRepo = context.application.bot_data["users_repo"]
    groups: GroupsRepo = context.application.bot_data["groups_repo"]
    friends: FriendsRepo = context.application.bot_data["friends_repo"]

    uid = update.effective_user.id
    uname = (update.effective_user.username or "").lower()

    # helpers (keep simple)

    def _icon_registered(user_id: Optional[int]) -> str:
        return "✅" if user_id else "⚪️"

    def _fmt_bday(d, m, y) -> str:
        if d and m:
            return f"{int(d):02d}-{int(m):02d}" + (f"-{int(y)}" if y else "")
        return t("when_unknown", update=update, context=context)

    def _days_key(d, m) -> int:
        if not d or not m:
            return 10**9
        import datetime as dt
        today = dt.date.today()
        try:
            tdate = dt.date(today.year, int(m), int(d))
        except Exception:
            return 10**9
        if tdate < today:
            tdate = tdate.replace(year=today.year + 1)
        return (tdate - today).days

    def _when(days: int) -> str:
        if days == 0:
            return t("when_today", update=update, context=context)
        if days >= 10**8:
            return t("when_unknown", update=update, context=context)
        return t("when_in_days", update=update, context=context, n=days)

    contacts: Dict[Tuple[Optional[int], Optional[str]], Dict] = {}

    # collect friends
    try:
        f_rows = await friends.list_for_user(uid)
        for r in f_rows:
            d = dict(r)
            key = (
                d.get("friend_user_id"),
                (d.get("friend_username") or "").lower() if d.get("friend_username") else None,
            )
            p = contacts.get(key) or {
                "user_id": d.get("friend_user_id"),
                "username": d.get("friend_username"),
                "birth_day": d.get("birth_day"),
                "birth_month": d.get("birth_month"),
                "birth_year": d.get("birth_year"),
                "as_friend": True,
                "as_group": False,
            }
            p["as_friend"] = True
            p["birth_day"] = p["birth_day"] or d.get("birth_day")
            p["birth_month"] = p["birth_month"] or d.get("birth_month")
            p["birth_year"] = p["birth_year"] or d.get("birth_year")
            contacts[key] = p
    except Exception as e:
        log.exception("friends fetch failed: %s", e)

    # collect co-members from groups
    try:
        g_rows = await groups.list_user_groups(uid)
        for g in g_rows:
            members = await groups.list_members(g["group_id"])
            for m in members:
                md = dict(m)
                mid = md.get("user_id")
                mname = (md.get("username") or "").lower() if md.get("username") else None
                # skip self
                if (mid and mid == uid) or (not mid and mname and mname == uname):
                    continue
                key = (mid, mname)
                p = contacts.get(key) or {
                    "user_id": mid,
                    "username": md.get("username"),
                    "birth_day": md.get("birth_day"),
                    "birth_month": md.get("birth_month"),
                    "birth_year": md.get("birth_year"),
                    "as_friend": False,
                    "as_group": True,
                }
                p["as_group"] = True
                p["birth_day"] = p["birth_day"] or md.get("birth_day")
                p["birth_month"] = p["birth_month"] or md.get("birth_month")
                p["birth_year"] = p["birth_year"] or md.get("birth_year")
                contacts[key] = p
    except Exception as e:
        log.exception("groups fetch failed: %s", e)

    if not contacts:
        await update.message.reply_text(
            t("birthdays_empty", update=update, context=context),
            reply_markup=main_menu_kb(update=update, context=context),
        )
        return

    items = list(contacts.values())
    items.sort(key=lambda x: _days_key(x.get("birth_day"), x.get("birth_month")))

    lines = [t("birthdays_header", update=update, context=context)]
    for x in items:
        icon = _icon_registered(x.get("user_id"))
        name = f"@{x['username']}" if x.get("username") else (f"id:{x['user_id']}" if x.get("user_id") else "unknown")
        bd = _fmt_bday(x.get("birth_day"), x.get("birth_month"), x.get("birth_year"))
        dleft = _days_key(x.get("birth_day"), x.get("birth_month"))
        when = _when(dleft)
        tags = []
        if x.get("as_friend"):
            tags.append(t("badge_friend", update=update, context=context))
        if x.get("as_group"):
            tags.append(t("badge_in_group", update=update, context=context))
        tag_str = f" [{' & '.join(tags)}]" if tags else ""
        lines.append(f"• {icon} {name} — {bd} ({when}){tag_str}")

    await update.message.reply_text("\n".join(lines), reply_markup=main_menu_kb(update=update, context=context))


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
    await update.message.reply_text(f"test: sent {sent} alerts.")

async def who_follows_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notif: NotifService = context.application.bot_data.get("notif_service")  # type: ignore
    if not notif:
        await update.message.reply_text("notif service not ready.")
        return
    txt = await notif.debug_followers(update.effective_user.id)
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

    # errors
    app.add_error_handler(on_error)

    # start / registration (+ language pick)
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("start", start_handler.start)],
            states={
                AWAITING_REGISTRATION_BDAY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, start_handler.reg_bday_entered)
                ],
                AWAITING_LANG_PICK: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, start_handler.lang_pick_entered)
                ],
            },
            fallbacks=[],
            name="conv_start_reg",
            persistent=False,
        ),
        group=0,
    )

    # main menu buttons (all through btn_regex -> i18n-proof)
    app.add_handler(MessageHandler(filters.Regex(btn_regex("btn_birthdays")), show_birthdays), group=0)

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
    for ch in settings_handler.conversation_handlers():
        app.add_handler(ch, group=2)

    # language is inside settings handler as a convo (S_WAIT_LANG)

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
