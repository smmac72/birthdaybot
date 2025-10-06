from __future__ import annotations

import logging
import re
import datetime as dt
from typing import Optional, Tuple

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from ..db.repo_users import UsersRepo
from ..db.repo_friends import FriendsRepo
from ..db.repo_groups import GroupsRepo
from ..keyboards import settings_menu_kb, main_menu_kb
from ..i18n import t, btn_regex, language_button_text, parse_language_choice, available_languages, set_lang, current_lang, language_label

# states
S_WAIT_BDAY = 0
S_WAIT_TZ = 1
S_WAIT_ALERT = 2
S_WAIT_LANG = 3

log = logging.getLogger("settings")


def _cancel_kb(update=None, context=None) -> ReplyKeyboardMarkup:
    # force replace any previous keyboard during input steps
    return ReplyKeyboardMarkup([[t("btn_cancel", update=update, context=context)]], resize_keyboard=True, one_time_keyboard=True)


def _fmt_bday(d: Optional[int], m: Optional[int], y: Optional[int], *, update=None, context=None) -> str:
    if d and m:
        return f"{int(d):02d}-{int(m):02d}" + (f"-{int(y)}" if y else "")
    return t("when_unknown", update=update, context=context)


def _parse_bday(text: str) -> Optional[Tuple[int, int, Optional[int]]]:
    s = (text or "").strip()
    m = re.search(r"\b(\d{2})-(\d{2})(?:-(\d{4}))?\b", s)
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


def _is_leap(year: int) -> bool:
    return year % 400 == 0 or (year % 4 == 0 and year % 100 != 0)


def _valid_calendar_date(d: int, m: int, y: Optional[int]) -> bool:
    yy = y if y is not None else 2000
    try:
        dt.date(yy, m, d)
        return True
    except ValueError:
        return False


def _normalize_bday(d: int, m: int, y: Optional[int]) -> Tuple[int, int, Optional[int], Optional[str]]:
    if _valid_calendar_date(d, m, y):
        return d, m, y, "ok"
    if y is not None and m == 2 and d == 29 and not _is_leap(y):
        return d, m, None, "drop_year"
    return d, m, y, "bad"


def _gmt_label(tz_val) -> str:
    try:
        z = int(tz_val)
    except Exception:
        z = 0
    sign = "+" if z >= 0 else ""
    return f"GMT{sign}{z}"


def _when_str(days: int, *, update=None, context=None) -> str:
    if days == 0:
        return t("when_today", update=update, context=context)
    if days >= 10**8:
        return t("when_unknown", update=update, context=context)
    return t("when_in_days", update=update, context=context, n=days)


def _days_until_key(d: Optional[int], m: Optional[int]) -> int:
    if not d or not m:
        return 10**9
    today = dt.date.today()
    try:
        target = dt.date(today.year, int(m), int(d))
    except ValueError:
        return 10**9
    if target < today:
        target = target.replace(year=today.year + 1)
    return (target - today).days


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
            await update.message.reply_text(t("need_start", update=update, context=context), reply_markup=main_menu_kb(update=update, context=context))
            return

        # followers via friends
        followers_friends = 0
        try:
            followers_friends = await self.friends.count_followers(user_id=uid, username_lower=uname_l or None)
        except Exception as e:
            self.log.exception("followers friends count failed: %s", e)

        # followers via groups
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

        followers_total = int(followers_friends) + int(followers_groups)

        lines = [
            t("settings_header", update=update, context=context),
            t("settings_bday", update=update, context=context, bday=bd),
            t("settings_tz", update=update, context=context, tz=tz_lbl),
            t("settings_alert", update=update, context=context, h=alert),
            t("settings_followers_total", update=update, context=context,
              f_friends=followers_friends, f_groups=followers_groups, f_total=followers_total),
            "",
            t("choose_action", update=update, context=context),
        ]
        await update.message.reply_text("\n".join(lines), reply_markup=settings_menu_kb(update=update, context=context))

    # change birthday
    async def set_bday_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(t("settings_bday_prompt", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
        return S_WAIT_BDAY

    async def set_bday_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        if text == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(t("canceled", update=update, context=context), reply_markup=settings_menu_kb(update=update, context=context))
            return ConversationHandler.END

        b = _parse_bday(text)
        if not b:
            await update.message.reply_text(t("settings_bday_bad", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
            return S_WAIT_BDAY

        d0, m0, y0 = b
        d, m, y, note = _normalize_bday(d0, m0, y0)
        if note == "bad":
            await update.message.reply_text(t("settings_bday_bad", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
            return S_WAIT_BDAY

        try:
            await self.users.update_bday(uid, d, m, y)
            if note == "drop_year":
                await update.message.reply_text(t("settings_bday_ok_dropped_year", update=update, context=context, bday=_fmt_bday(d, m, None, update=update, context=context)), reply_markup=settings_menu_kb(update=update, context=context))
            else:
                await update.message.reply_text(t("settings_bday_ok", update=update, context=context), reply_markup=settings_menu_kb(update=update, context=context))
        except Exception as e:
            self.log.exception("set_bday failed: %s", e)
            await update.message.reply_text(t("settings_bday_fail", update=update, context=context), reply_markup=settings_menu_kb(update=update, context=context))

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
        await update.message.reply_text(t("settings_tz_prompt", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
        return S_WAIT_TZ

    async def set_tz_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        if text == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(t("canceled", update=update, context=context), reply_markup=settings_menu_kb(update=update, context=context))
            return ConversationHandler.END

        try:
            tz = int(text)
            if tz < -12 or tz > 14:
                raise ValueError("out of range")
        except Exception:
            await update.message.reply_text(t("settings_tz_bad", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
            return S_WAIT_TZ

        try:
            await self.users.update_tz(uid, tz)
            await update.message.reply_text(t("settings_tz_ok", update=update, context=context), reply_markup=settings_menu_kb(update=update, context=context))
        except Exception as e:
            self.log.exception("set_tz failed: %s", e)
            await update.message.reply_text(t("settings_tz_fail", update=update, context=context), reply_markup=settings_menu_kb(update=update, context=context))

        notif = context.application.bot_data.get("notif_service")
        if notif:
            try:
                await notif.reschedule_for_follower(uid)
            except Exception as e:
                self.log.exception("reschedule_for_follower after tz failed: %s", e)

        return ConversationHandler.END

    # change alert hours
    async def set_alert_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(t("settings_alert_prompt", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
        return S_WAIT_ALERT

    async def set_alert_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        if text == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(t("canceled", update=update, context=context), reply_markup=settings_menu_kb(update=update, context=context))
            return ConversationHandler.END

        try:
            h = int(text)
            if h < 0 or h > 48:
                raise ValueError("out")
        except Exception:
            await update.message.reply_text(t("settings_alert_bad", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
            return S_WAIT_ALERT

        try:
            await self.users.update_alert_hours(uid, h)
            await update.message.reply_text(t("settings_alert_ok", update=update, context=context), reply_markup=settings_menu_kb(update=update, context=context))
        except Exception as e:
            self.log.exception("set_alert failed: %s", e)
            await update.message.reply_text(t("settings_alert_fail", update=update, context=context), reply_markup=settings_menu_kb(update=update, context=context))

        notif = context.application.bot_data.get("notif_service")
        if notif:
            try:
                await notif.reschedule_for_follower(uid)
            except Exception as e:
                self.log.exception("reschedule_for_follower after alert failed: %s", e)

        return ConversationHandler.END

    # language change
    async def change_lang_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cur = current_lang(update=update, context=context)
        lbl = language_label(cur)
        rows = [[language_button_text(c)] for c in (available_languages() or ["ru", "en"])]
        rows.append([t("btn_cancel", update=update, context=context)])
        kb = ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(t("settings_lang_pick", update=update, context=context, lang=lbl), reply_markup=kb)

        return S_WAIT_LANG

    async def change_lang_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        choice = (update.message.text or "").strip()
        if choice == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(t("canceled", update=update, context=context), reply_markup=settings_menu_kb(update=update, context=context))
            return ConversationHandler.END

        code = parse_language_choice(choice)
        if not code:
            rows = [[language_button_text(c)] for c in (available_languages() or ["ru", "en"])]
            rows.append([t("btn_cancel", update=update, context=context)])
            kb = ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)
            await update.message.reply_text(t("settings_lang_bad", update=update, context=context), reply_markup=kb)
            return S_WAIT_LANG

        set_lang(code, context=context)
        lbl = language_label(code)
        await update.message.reply_text(
            t("settings_lang_set_ok", update=update, context=context, lang=lbl),
            reply_markup=settings_menu_kb(update=update, context=context),
        )
        return ConversationHandler.END

    # factory
    def conversation_handlers(self):
        return [
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(btn_regex("btn_settings_bday")), self.set_bday_start)],
                states={S_WAIT_BDAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_bday_wait)]},
                fallbacks=[],
                name="conv_settings_bday",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(btn_regex("btn_settings_tz")), self.set_tz_start)],
                states={S_WAIT_TZ: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_tz_wait)]},
                fallbacks=[],
                name="conv_settings_tz",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(btn_regex("btn_settings_alert")), self.set_alert_start)],
                states={S_WAIT_ALERT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_alert_wait)]},
                fallbacks=[],
                name="conv_settings_alert",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(btn_regex("btn_settings_lang")), self.change_lang_start)],
                states={S_WAIT_LANG: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.change_lang_wait)]},
                fallbacks=[],
                name="conv_settings_lang",
                persistent=False,
            ),
        ]
