from __future__ import annotations

import logging
import re
import datetime as dt
import uuid
from typing import Optional

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from ..db.repo_friends import FriendsRepo
from ..db.repo_users import UsersRepo
from ..db.repo_groups import GroupsRepo
from ..keyboards import friends_menu_kb
from ..i18n import t, btn_regex

# states
STATE_WAIT_ADD = 0
STATE_WAIT_ADD_DATE = 1
STATE_WAIT_DELETE = 2

def _log_id() -> str:
    return uuid.uuid4().hex[:8]

def _cancel_kb(update=None, context=None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[t("btn_cancel", update=update, context=context)]], resize_keyboard=True, one_time_keyboard=True)

def _icon_registered(user_id: Optional[int]) -> str:
    return "✅" if user_id else "⚪️"

def _when_str(days: int, *, update=None, context=None) -> str:
    if days == 0:
        return t("when_today", update=update, context=context)
    if days >= 10**8:
        return t("when_unknown", update=update, context=context)
    return t("when_in_days", update=update, context=context, n=days)

def _fmt_bday(d: Optional[int], m: Optional[int], y: Optional[int], *, update=None, context=None) -> str:
    if d and m:
        return f"{int(d):02d}-{int(m):02d}" + (f"-{int(y)}" if y else "")
    return t("when_unknown", update=update, context=context)

def _parse_bday(text: str):
    ttxt = (text or "").strip()
    m = re.search(r"\b(\d{2})-(\d{2})(?:-(\d{4}))?\b", ttxt)
    if not m:
        return None
    d = int(m.group(1)); mo = int(m.group(2))
    y = int(m.group(3)) if m.group(3) else None
    if not (1 <= d <= 31 and 1 <= mo <= 12):
        return None
    if y is not None and (y < 1900 or y > 2100):
        return None
    # soft validate calendar
    try:
        _ = dt.date(y if y else 2000, mo, d)
    except ValueError:
        if not (mo == 2 and d == 29):
            return None
        # 29 feb without leap-year -> accept by dropping year later
    return d, mo, y

def _days_until_key(d: Optional[int], m: Optional[int]) -> int:
    if not d or not m:
        return 10**9
    today = dt.date.today()
    try:
        target = dt.date(today.year, m, d)
    except ValueError:
        return 10**9
    if target < today:
        target = target.replace(year=today.year + 1)
    return (target - today).days

# ----- safe tz parsing (fix for 'UTC', 'GMT-4', '+3', etc.) -----
_TZ_INT_RE = re.compile(r"([+-]?\d{1,2})")

def _as_int(v, default: int = 0) -> int:
    """
    Convert various tz representations to int hours.
    Accepts: 3, -5, '0', 'UTC', 'UTC+2', 'GMT-4', '+3'
    """
    if v is None:
        return default
    if isinstance(v, int):
        return v
    try:
        if isinstance(v, str):
            s = v.strip()
            # direct int in string
            try:
                return int(s)
            except Exception:
                pass
            # common tokens like 'UTC', 'GMT'
            if s.upper() in ("UTC", "GMT"):
                return 0
            m = _TZ_INT_RE.search(s)
            if m:
                return int(m.group(1))
            return default
        # last resort
        return int(v)
    except Exception:
        return default
# ----------------------------------------------------------------

class FriendsHandler:
    def __init__(self, users: UsersRepo, friends: FriendsRepo, groups: GroupsRepo):
        self.friends = friends
        self.users = users
        self.groups = groups
        self.log = logging.getLogger("friends")

    # menu entry shows the list immediately
    async def menu_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        rid = _log_id()
        uid = update.effective_user.id
        self.log.info("[%s] friends entry: uid=%s", rid, uid)

        try:
            rows = await self.friends.list_for_user(uid)
            rows = [dict(r) for r in rows]
        except Exception as e:
            self.log.exception("[%s] list_friends failed: %s", rid, e)
            await update.message.reply_text(t("not_found", update=update, context=context), reply_markup=friends_menu_kb(update=update, context=context))
            return

        # sort by days until (no tz needed here)
        rows.sort(key=lambda r: _days_until_key(r.get("birth_day"), r.get("birth_month")))

        lines = [t("friends_header", update=update, context=context)] if rows else [
            t("friends_empty", update=update, context=context)
        ]
        for r in rows:
            icon = _icon_registered(r.get("friend_user_id"))
            name = f"@{r['friend_username']}" if r.get("friend_username") else (
                f"id:{r['friend_user_id']}" if r.get("friend_user_id") else "unknown"
            )
            bd = _fmt_bday(r.get("birth_day"), r.get("birth_month"), r.get("birth_year"), update=update, context=context)
            dleft = _days_until_key(r.get("birth_day"), r.get("birth_month"))
            when = _when_str(dleft, update=update, context=context)
            lines.append(f"{icon} {name} — {bd} ({when})")

        await update.message.reply_text("\n".join(lines), reply_markup=friends_menu_kb(update=update, context=context))

    # add friend
    async def add_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            t("friends_add_prompt", update=update, context=context),
            reply_markup=_cancel_kb(update=update, context=context),
        )
        return STATE_WAIT_ADD

    async def add_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        if text == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(t("canceled", update=update, context=context), reply_markup=friends_menu_kb(update=update, context=context))
            return ConversationHandler.END

        parts = text.split()
        username = None; user_id: Optional[int] = None
        if parts:
            if parts[0].startswith("@"):
                username = parts[0][1:]
            elif parts[0].isdigit():
                try:
                    user_id = int(parts[0])
                except Exception:
                    user_id = None

        bday = _parse_bday(text)

        # resolve profile if user exists in bot
        prof = None
        if user_id:
            prof = await self.users.get_user(user_id)
        elif username:
            prof = await self.users.get_user_by_username(username)
        prof = dict(prof) if prof else None

        notif = context.application.bot_data.get("notif_service")

        if prof:
            await self.friends.add_friend(
                uid,
                friend_user_id=prof.get("user_id"),
                friend_username=prof.get("username"),
            )
            # reschedule: friend person (priority item 1)
            if notif:
                try:
                    await notif.reschedule_for_person(prof.get("user_id"), prof.get("username"))
                except Exception as e:
                    self.log.exception("add friend reschedule failed: %s", e)

            await update.message.reply_text(t("friends_add_ok", update=update, context=context), reply_markup=friends_menu_kb(update=update, context=context))
            return ConversationHandler.END

        if bday:
            d, mo, y = bday
            # soft fix: if 29-02 with non-leap year -> drop year
            try:
                if y is not None:
                    dt.date(y, mo, d)
            except ValueError:
                if mo == 2 and d == 29:
                    y = None
                else:
                    await update.message.reply_text(t("friends_add_date_bad", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
                    return STATE_WAIT_ADD

            await self.friends.add_friend(
                uid,
                friend_user_id=user_id,
                friend_username=username,
                birth_day=d, birth_month=mo, birth_year=y,
            )
            # reschedule: nothing to schedule for person if no id
            await update.message.reply_text(t("friends_add_ok", update=update, context=context), reply_markup=friends_menu_kb(update=update, context=context))
            return ConversationHandler.END

        context.user_data["pending_friend"] = {"user_id": user_id, "username": username}
        await update.message.reply_text(
            t("friends_add_date_prompt", update=update, context=context),
            reply_markup=_cancel_kb(update=update, context=context),
        )
        return STATE_WAIT_ADD_DATE

    async def add_wait_date(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        if text == t("btn_cancel", update=update, context=context):
            context.user_data.pop("pending_friend", None)
            await update.message.reply_text(t("canceled", update=update, context=context), reply_markup=friends_menu_kb(update=update, context=context))
            return ConversationHandler.END

        bday = _parse_bday(text)
        if not bday:
            await update.message.reply_text(t("friends_add_date_bad", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
            return STATE_WAIT_ADD_DATE

        d, mo, y = bday
        # soft fix invalid calendars similar to above
        try:
            if y:
                dt.date(y, mo, d)
        except ValueError:
            if not (mo == 2 and d == 29):
                await update.message.reply_text(t("friends_add_date_bad", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
                return STATE_WAIT_ADD_DATE
            y = None

        pending = context.user_data.get("pending_friend") or {}
        username = pending.get("username"); user_id = pending.get("user_id")

        await self.friends.add_friend(
            uid,
            friend_user_id=user_id,
            friend_username=username,
            birth_day=d, birth_month=mo, birth_year=y,
        )
        context.user_data.pop("pending_friend", None)

        await update.message.reply_text(t("friends_add_ok", update=update, context=context), reply_markup=friends_menu_kb(update=update, context=context))
        return ConversationHandler.END

    # delete friend
    async def delete_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(t("friends_del_prompt", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
        return STATE_WAIT_DELETE

    async def delete_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        if text == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(t("canceled", update=update, context=context), reply_markup=friends_menu_kb(update=update, context=context))
            return ConversationHandler.END

        username = text[1:] if text.startswith("@") else None
        user_id = None
        if text.isdigit():
            try:
                user_id = int(text)
            except Exception:
                user_id = None

        ok = False
        try:
            if user_id:
                ok = await self.friends.delete_friend(uid, friend_user_id=user_id)
            elif username:
                ok = await self.friends.delete_friend(uid, friend_username=username)
        except Exception:
            ok = False

        # reschedule for person if we know exact id (priority item 1)
        notif = context.application.bot_data.get("notif_service")
        if notif and user_id:
            try:
                await notif.reschedule_for_person(user_id)
            except Exception as e:
                self.log.exception("delete friend reschedule failed: %s", e)

        await update.message.reply_text(t("friends_del_ok", update=update, context=context) if ok else t("friends_del_fail", update=update, context=context), reply_markup=friends_menu_kb(update=update, context=context))
        return ConversationHandler.END

    # conv factories
    def conversation_handlers(self):
        return [
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(btn_regex("btn_friend_add")), self.add_start)],
                states={
                    STATE_WAIT_ADD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_wait)],
                    STATE_WAIT_ADD_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_wait_date)],
                },
                fallbacks=[MessageHandler(filters.Regex(btn_regex("btn_cancel")), self.menu_entry)],
                name="conv_friends_add",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(btn_regex("btn_friend_del")), self.delete_start)],
                states={STATE_WAIT_DELETE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.delete_wait)]},
                fallbacks=[MessageHandler(filters.Regex(btn_regex("btn_cancel")), self.menu_entry)],
                name="conv_friends_delete",
                persistent=False,
            ),
        ]
