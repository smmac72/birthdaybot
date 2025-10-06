from __future__ import annotations
import logging
import re
import uuid
from typing import Optional

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from ..db.repo_friends import FriendsRepo
from ..db.repo_users import UsersRepo
from ..db.repo_groups import GroupsRepo
from ..keyboards import friends_menu_kb

# states
STATE_WAIT_ADD = 0
STATE_WAIT_ADD_DATE = 1
STATE_WAIT_DELETE = 2

def _log_id() -> str:
    return uuid.uuid4().hex[:8]

def _cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["◀️ отмена"]], resize_keyboard=True, one_time_keyboard=True)

def _icon_registered(user_id: Optional[int]) -> str:
    return "✅" if user_id else "⚪️"

def _fmt_bday(d: Optional[int], m: Optional[int], y: Optional[int]) -> str:
    if d and m:
        return f"{int(d):02d}-{int(m):02d}" + (f"-{int(y)}" if y else "")
    return "не указан"

def _valid_date(d: int, m: int, y: Optional[int]) -> bool:
    import datetime as dt
    try:
        if y is None:
            y = 2000
        dt.date(int(y), int(m), int(d))
        return True
    except Exception:
        return False

def _parse_bday(text: str):
    t = (text or "").strip()
    m = re.search(r"\b(\d{2})-(\d{2})(?:-(\d{4}))?\b", t)
    if not m:
        return None
    d = int(m.group(1)); mo = int(m.group(2))
    y = int(m.group(3)) if m.group(3) else None
    if not (1 <= d <= 31 and 1 <= mo <= 12):
        return None
    if y is not None and (y < 1900 or y > 2100):
        return None
    if not _valid_date(d, mo, y):
        return None
    return d, mo, y

def _days_until_key(d: Optional[int], m: Optional[int]) -> int:
    if not d or not m:
        return 10**9
    import datetime as dt
    today = dt.date.today()
    try:
        target = dt.date(today.year, m, d)
    except ValueError:
        return 10**9
    if target < today:
        target = target.replace(year=today.year + 1)
    return (target - today).days

def _when_str(days: int) -> str:
    if days == 0:
        return "сегодня"
    if days >= 10**8:
        return "дата не указана"
    return f"через {days} дн."

class FriendsHandler:
    def __init__(self, users: UsersRepo, friends: FriendsRepo, groups: GroupsRepo):
        self.friends = friends
        self.users = users
        self.groups = groups
        self.log = logging.getLogger("friends")

    async def menu_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        rid = _log_id()
        uid = update.effective_user.id
        self.log.info("[%s] friends entry: uid=%s", rid, uid)

        try:
            rows = await self.friends.list_for_user(uid)
            rows = [dict(r) for r in rows]
        except Exception as e:
            self.log.exception("[%s] list_friends failed: %s", rid, e)
            await update.message.reply_text("не удалось получить список друзей.", reply_markup=friends_menu_kb())
            return

        rows.sort(key=lambda r: _days_until_key(r.get("birth_day"), r.get("birth_month")))

        lines = ["друзья:\n"] if rows else [
            "у вас пока нет друзей.\nдобавьте друга — и мы начнём напоминать о его дне рождения."
        ]
        for r in rows:
            icon = _icon_registered(r.get("friend_user_id"))
            name = f"@{r['friend_username']}" if r.get("friend_username") else (
                f"id:{r['friend_user_id']}" if r.get("friend_user_id") else "unknown"
            )
            bd = _fmt_bday(r.get("birth_day"), r.get("birth_month"), r.get("birth_year"))
            dleft = _days_until_key(r.get("birth_day"), r.get("birth_month"))
            when = _when_str(dleft)
            lines.append(f"{icon} {name} — {bd} ({when})")

        await update.message.reply_text("\n".join(lines), reply_markup=friends_menu_kb())

    # add friend
    async def add_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "введите @username или id. можно одной строкой: @username дд-мм(-гггг).",
            reply_markup=_cancel_kb(),
        )
        return STATE_WAIT_ADD

    async def add_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        if text == "◀️ отмена":
            await update.message.reply_text("отменено.", reply_markup=friends_menu_kb())
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

        if prof:
            await self.friends.add_friend(
                uid,
                friend_user_id=prof.get("user_id"),
                friend_username=prof.get("username"),
            )
            # reschedule: target person only (alerts for their followers)
            notif = context.application.bot_data.get("notif_service")
            if notif:
                try:
                    await notif.reschedule_for_person(prof.get("user_id"), prof.get("username"))
                except Exception as e:
                    self.log.exception("add friend reschedule failed: %s", e)

            await update.message.reply_text("друг добавлен.", reply_markup=friends_menu_kb())
            return ConversationHandler.END

        if bday:
            d, m, y = bday
            await self.friends.add_friend(
                uid,
                friend_user_id=user_id,
                friend_username=username,
                birth_day=d, birth_month=m, birth_year=y,
            )
            # no reschedule_for_person if user not registered (no id to target)
            await update.message.reply_text("друг добавлен.", reply_markup=friends_menu_kb())
            return ConversationHandler.END

        context.user_data["pending_friend"] = {"user_id": user_id, "username": username}
        await update.message.reply_text(
            "этот пользователь не найден в боте. укажите дату рождения (дд-мм-гггг или дд-мм):",
            reply_markup=_cancel_kb(),
        )
        return STATE_WAIT_ADD_DATE

    async def add_wait_date(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        if text == "◀️ отмена":
            context.user_data.pop("pending_friend", None)
            await update.message.reply_text("отменено.", reply_markup=friends_menu_kb())
            return ConversationHandler.END

        bday = _parse_bday(text)
        if not bday:
            await update.message.reply_text("неверный формат. пример: 11-11 или 11-11-1999", reply_markup=_cancel_kb())
            return STATE_WAIT_ADD_DATE

        pending = context.user_data.get("pending_friend") or {}
        username = pending.get("username"); user_id = pending.get("user_id")
        d, m, y = bday

        await self.friends.add_friend(
            uid,
            friend_user_id=user_id,
            friend_username=username,
            birth_day=d, birth_month=m, birth_year=y,
        )
        context.user_data.pop("pending_friend", None)

        await update.message.reply_text("друг добавлен.", reply_markup=friends_menu_kb())
        return ConversationHandler.END

    # delete friend
    async def delete_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("введите @username или id для удаления:", reply_markup=_cancel_kb())
        return STATE_WAIT_DELETE

    async def delete_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        if text == "◀️ отмена":
            await update.message.reply_text("отменено.", reply_markup=friends_menu_kb())
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

        # reschedule for person (if we removed a registered friend)
        notif = context.application.bot_data.get("notif_service")
        if notif and user_id:
            try:
                await notif.reschedule_for_person(user_id)
            except Exception as e:
                self.log.exception("delete friend reschedule failed: %s", e)

        await update.message.reply_text("друг удалён." if ok else "не удалось найти такого друга.", reply_markup=friends_menu_kb())
        return ConversationHandler.END

    def conversation_handlers(self):
        return [
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex("^➕ добавить друга$"), self.add_start)],
                states={
                    STATE_WAIT_ADD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_wait)],
                    STATE_WAIT_ADD_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_wait_date)],
                },
                fallbacks=[MessageHandler(filters.Regex("^◀️ отмена$"), self.menu_entry)],
                name="conv_friends_add",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex("^➖ удалить друга$"), self.delete_start)],
                states={STATE_WAIT_DELETE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.delete_wait)]},
                fallbacks=[MessageHandler(filters.Regex("^◀️ отмена$"), self.menu_entry)],
                name="conv_friends_delete",
                persistent=False,
            ),
        ]
