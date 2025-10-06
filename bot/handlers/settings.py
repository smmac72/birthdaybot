from __future__ import annotations

import logging
import re
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from ..db.repo_users import UsersRepo
from ..db.repo_friends import FriendsRepo
from ..db.repo_groups import GroupsRepo
from ..keyboards import settings_menu_kb, main_menu_kb

# states
S_WAIT_BDAY = 0
S_WAIT_TZ = 1
S_WAIT_ALERT = 2

log = logging.getLogger("settings")


def _fmt_bday(d: Optional[int], m: Optional[int], y: Optional[int]) -> str:
    if d and m:
        return f"{int(d):02d}-{int(m):02d}" + (f"-{int(y)}" if y else "")
    return "не указан"


def _parse_bday(text: str):
    # accepts dd-mm or dd-mm-yyyy
    t = (text or "").strip()
    m = re.search(r"\b(\d{2})-(\d{2})(?:-(\d{4}))?\b", t)
    if not m:
        return None
    d = int(m.group(1))
    mo = int(m.group(2))
    y = int(m.group(3)) if m.group(3) else None
    if not (1 <= d <= 31 and 1 <= mo <= 12):
        return None
    if y is not None and (y < 1900 or y > 2100):
        return None
    return d, mo, y


def _gmt_label(tz_val) -> str:
    try:
        z = int(tz_val)
    except Exception:
        z = 0
    sign = "+" if z >= 0 else ""
    return f"gmt{sign}{z}"


class SettingsHandler:
    def __init__(self, users: UsersRepo, friends: FriendsRepo, groups: GroupsRepo):
        self.users = users
        self.friends = friends
        self.groups = groups
        self.log = logging.getLogger("settings")

    # menu entry
    async def menu_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        uname_l = (update.effective_user.username or "").lower()

        # load profile
        u = await self.users.get_user(uid)
        if not u:
            await update.message.reply_text("сначала нажмите /start", reply_markup=main_menu_kb())
            return

        # followers via friends
        followers_friends = 0
        try:
            followers_friends = await self.friends.count_followers(user_id=uid, username_lower=uname_l or None)
        except Exception as e:
            self.log.exception("followers friends count failed: %s", e)

        # followers via groups (co-members)
        followers_groups = 0
        try:
            groups = await self.groups.list_user_groups(uid)
            seen = set()
            for g in groups:
                members = await self.groups.list_members(g["group_id"])
                for m in members:
                    mid = m.get("user_id")
                    if isinstance(mid, int) and mid and mid != uid:
                        seen.add(int(mid))
            followers_groups = len(seen)
        except Exception as e:
            self.log.exception("followers groups count failed: %s", e)

        # fields
        bd = _fmt_bday(u.get("birth_day"), u.get("birth_month"), u.get("birth_year"))
        tz_lbl = _gmt_label(u.get("tz", 0))
        alert = u.get("alert_hours")
        try:
            alert = int(alert) if alert is not None else 0
        except Exception:
            alert = 0

        # text
        lines = [
            "ваши настройки:\n",
            f"дата рождения: {bd}",
            f"часовой пояс: {tz_lbl}",
            f"время уведомлений: за {alert} ч. до полуночи дня рождения",
            f"за вами следят: друзей — {followers_friends} | в группах — {followers_groups}",
            "",
            "выберите действие:",
        ]
        await update.message.reply_text("\n".join(lines), reply_markup=settings_menu_kb())

    # change birthday
    async def set_bday_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("введите дату как ДД-ММ или ДД-ММ-ГГГГ")
        return S_WAIT_BDAY

    async def set_bday_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        b = _parse_bday(text)
        if not b:
            await update.message.reply_text("неверный формат. пример: 07-02 или 07-02-2002")
            return S_WAIT_BDAY

        d, m, y = b
        try:
            await self.users.update_bday(uid, d, m, y)
            await update.message.reply_text("дата обновлена.", reply_markup=settings_menu_kb())
        except Exception as e:
            self.log.exception("set_bday failed: %s", e)
            await update.message.reply_text("не удалось обновить дату.", reply_markup=settings_menu_kb())

        # reschedule for person
        notif = context.application.bot_data.get("notif_service")
        if notif:
            try:
                await notif.reschedule_for_person(uid, update.effective_user.username)
            except Exception as e:
                self.log.exception("reschedule_for_person after bday failed: %s", e)

        return ConversationHandler.END

    # change timezone
    async def set_tz_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("введите смещение gmt, например: 3, 0, -5")
        return S_WAIT_TZ

    async def set_tz_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        try:
            tz = int(text)
            if tz < -12 or tz > 14:
                raise ValueError("out of range")
        except Exception:
            await update.message.reply_text("укажите целое число от -12 до 14")
            return S_WAIT_TZ

        try:
            await self.users.update_tz(uid, tz)
            await update.message.reply_text("часовой пояс обновлён.", reply_markup=settings_menu_kb())
        except Exception as e:
            self.log.exception("set_tz failed: %s", e)
            await update.message.reply_text("не удалось обновить часовой пояс.", reply_markup=settings_menu_kb())

        # reschedule follower
        notif = context.application.bot_data.get("notif_service")
        if notif:
            try:
                await notif.reschedule_for_follower(uid)
            except Exception as e:
                self.log.exception("reschedule_for_follower after tz failed: %s", e)

        return ConversationHandler.END

    # change alert hours
    async def set_alert_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("за сколько часов напоминать? укажите число (0..48)")
        return S_WAIT_ALERT

    async def set_alert_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        try:
            h = int(text)
            if h < 0 or h > 48:
                raise ValueError("out")
        except Exception:
            await update.message.reply_text("введите целое число от 0 до 48")
            return S_WAIT_ALERT

        try:
            await self.users.update_alert_hours(uid, h)
            await update.message.reply_text("время уведомлений обновлено.", reply_markup=settings_menu_kb())
        except Exception as e:
            self.log.exception("set_alert failed: %s", e)
            await update.message.reply_text("не удалось обновить время уведомлений.", reply_markup=settings_menu_kb())

        # reschedule follower
        notif = context.application.bot_data.get("notif_service")
        if notif:
            try:
                await notif.reschedule_for_follower(uid)
            except Exception as e:
                self.log.exception("reschedule_for_follower after alert failed: %s", e)

        return ConversationHandler.END

    # stub
    async def change_lang(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("смена языка появится позже.", reply_markup=settings_menu_kb())

    # factory (optional, если регистрируешь явно в main.py — можно не использовать)
    def conversation_handlers(self):
        return [
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex("^дата рождения$"), self.set_bday_start)],
                states={S_WAIT_BDAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_bday_wait)]},
                fallbacks=[],
                name="conv_settings_bday",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex("^часовой пояс$"), self.set_tz_start)],
                states={S_WAIT_TZ: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_tz_wait)]},
                fallbacks=[],
                name="conv_settings_tz",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex("^отложенность$"), self.set_alert_start)],
                states={S_WAIT_ALERT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_alert_wait)]},
                fallbacks=[],
                name="conv_settings_alert",
                persistent=False,
            ),
            MessageHandler(filters.Regex("^язык$"), self.change_lang),
        ]
