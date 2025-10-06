from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from ..db.repo_users import UsersRepo
from ..keyboards import main_menu_kb

log = logging.getLogger("start")

# single state: waiting for birthday at registration
AWAITING_REGISTRATION_BDAY = 1

# helpers

def _valid_date(d: int, m: int, y: Optional[int]) -> bool:
    # chill check via datetime; accept 29 feb; reject impossible combos like 31-11
    import datetime as dt
    try:
        if y is None:
            # pick a leap year so 29 feb is ok
            y = 2000
        dt.date(int(y), int(m), int(d))
        return True
    except Exception:
        return False

def _parse_bday(text: str) -> Optional[Tuple[int, int, Optional[int]]]:
    # accepts dd-mm-yyyy or dd-mm
    t = (text or "").strip()
    parts = t.split("-")
    if len(parts) not in (2, 3):
        return None
    try:
        d = int(parts[0]); m = int(parts[1])
        y = int(parts[2]) if len(parts) == 3 else None
    except Exception:
        return None
    if not (1 <= d <= 31 and 1 <= m <= 12):
        return None
    if y is not None and (y < 1900 or y > 2100):
        return None
    if not _valid_date(d, m, y):
        return None
    return d, m, y

@dataclass
class StartHandler:
    users: UsersRepo

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # register user and optionally ask for birthday
        tg_user = update.effective_user
        chat_id = update.effective_chat.id if update.effective_chat else None

        u = await self.users.ensure_user(tg_user, chat_id=chat_id)
        if not u:
            await update.message.reply_text("что-то пошло не так. попробуйте позже.", reply_markup=main_menu_kb())
            return ConversationHandler.END

        app = context.application
        app.bot_data.setdefault("users_repo", self.users)

        has_bday = bool(u.get("birth_day") and u.get("birth_month"))
        if has_bday:
            await update.message.reply_text("с возвращением!", reply_markup=main_menu_kb())
            return ConversationHandler.END

        await update.message.reply_text(
            "введите дату рождения в формате дд-мм-гггг или дд-мм (например, 15-05-1990 или 15-05):\n\n"
            "нажмите «◀️ отмена» чтобы выйти.",
        )
        return AWAITING_REGISTRATION_BDAY

    async def reg_bday_entered(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "").strip()
        if text == "◀️ отмена":
            await update.message.reply_text("регистрация отменена.", reply_markup=main_menu_kb())
            return ConversationHandler.END

        parsed = _parse_bday(text)
        if not parsed:
            await update.message.reply_text("не получилось. пример: 07-02 или 07-02-2002")
            return AWAITING_REGISTRATION_BDAY

        d, m, y = parsed
        uid = update.effective_user.id

        try:
            # users repo in this tree uses update_bday, not set_birthday
            await self.users.update_bday(uid, d, m, y)
        except Exception as e:
            log.exception("failed to set birthday: %s", e)
            await update.message.reply_text("не удалось сохранить дату. попробуйте ещё раз.")
            return AWAITING_REGISTRATION_BDAY

        try:
            if update.effective_chat:
                await self.users.update_chat_id(uid, update.effective_chat.id)
        except Exception:
            pass

        # reschedule for person so followers get proper triggers
        notif = context.application.bot_data.get("notif_service")
        if notif:
            try:
                await notif.reschedule_for_person(uid, update.effective_user.username)
            except Exception as e:
                log.info("reschedule after start bday set failed: %s", e)

        await update.message.reply_text(
            f"ок, сохранил: {d:02d}-{m:02d}" + (f"-{y}" if y else "") + ".",
            reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END

def conversation():
    return ConversationHandler(
        entry_points=[],
        states={AWAITING_REGISTRATION_BDAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, StartHandler.reg_bday_entered)]},
        fallbacks=[],
        name="conv_start_reg",
        persistent=False,
    )
