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


# utils

def _parse_bday(text: str) -> Optional[Tuple[int, int, Optional[int]]]:
    # accepts dd-mm-yyyy or dd-mm
    t = (text or "").strip()
    parts = t.split("-")
    if len(parts) not in (2, 3):
        return None
    try:
        d = int(parts[0])
        m = int(parts[1])
        y = int(parts[2]) if len(parts) == 3 else None
    except Exception:
        return None
    if not (1 <= d <= 31 and 1 <= m <= 12):
        return None
    if y is not None and (y < 1900 or y > 2100):
        return None
    return d, m, y


@dataclass
class StartHandler:
    users: UsersRepo

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # register user, save chat id, and decide whether to ask for birthday
        tg_user = update.effective_user
        chat_id = update.effective_chat.id if update.effective_chat else None

        u = await self.users.ensure_user(tg_user, chat_id=chat_id)
        if not u:
            await update.message.reply_text("что-то пошло не так. попробуйте ещё раз позже.")
            return ConversationHandler.END

        # keep repos in app.bot_data (context.application, not update.application)
        app = context.application
        app.bot_data.setdefault("users_repo", self.users)

        # if birthday set, just show main menu
        has_bday = bool(u.get("birth_day") and u.get("birth_month"))
        if has_bday:
            await update.message.reply_text("с возвращением!", reply_markup=main_menu_kb())
            return ConversationHandler.END

        # ask for birthday
        await update.message.reply_text(
            "введите дату рождения в формате ДД-ММ-ГГГГ или ДД-ММ (например, 15-05-1990 или 15-05):\n\n"
            "нажмите «◀️ отмена» чтобы выйти.",
        )
        return AWAITING_REGISTRATION_BDAY

    async def reg_bday_entered(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # handle birthday input during registration
        text = (update.message.text or "").strip()
        if text == "◀️ отмена":
            await update.message.reply_text("регистрация отменена.", reply_markup=main_menu_kb())
            return ConversationHandler.END

        parsed = _parse_bday(text)
        if not parsed:
            await update.message.reply_text("неверный формат. введите ДД-ММ-ГГГГ или ДД-ММ:")
            return AWAITING_REGISTRATION_BDAY

        d, m, y = parsed
        uid = update.effective_user.id

        try:
            await self.users.set_birthday(uid, d, m, y)
        except Exception as e:
            log.exception("failed to set birthday: %s", e)
            await update.message.reply_text("не удалось сохранить дату. попробуйте ещё раз.")
            return AWAITING_REGISTRATION_BDAY

        # try to save chat id again to be safe
        try:
            if update.effective_chat:
                await self.users.update_chat_id(uid, update.effective_chat.id)
        except Exception:
            pass

        # best-effort propagation to friends/groups if repos are available
        app = context.application
        friends = app.bot_data.get("friends_repo")
        groups = app.bot_data.get("groups_repo")

        # propagate to direct followers if repo supports it
        try:
            if friends and hasattr(friends, "propagate_user_birthday"):
                await friends.propagate_user_birthday(user_id=uid, day=d, month=m, year=y)
        except Exception as e:
            log.info("friends propagation skipped: %s", e)

        # groups can cache birthdays; try to propagate if supported
        try:
            if groups and hasattr(groups, "propagate_user_birthday"):
                await groups.propagate_user_birthday(user_id=uid, day=d, month=m, year=y)
        except Exception as e:
            log.info("groups propagation skipped: %s", e)

        await update.message.reply_text(
            f"ок, сохранил дату: {d:02d}-{m:02d}" + (f"-{y}" if y else "") + ".",
            reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END


# helper to wire conversation in main if needed (optional)
def conversation():
    return ConversationHandler(
        entry_points=[],
        states={AWAITING_REGISTRATION_BDAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, StartHandler.reg_bday_entered)]},
        fallbacks=[],
        name="conv_start_reg",
        persistent=False,
    )
