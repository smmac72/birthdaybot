from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from ..db.repo_users import UsersRepo
from ..keyboards import main_menu_kb
from ..i18n import t, set_lang, available_languages, language_button_text, parse_language_choice

log = logging.getLogger("start")

# states
AWAITING_LANGUAGE = 0
AWAITING_REGISTRATION_BDAY = 1

# utils

def _parse_bday(text: str) -> Optional[Tuple[int, int, Optional[int]]]:
    # accepts dd-mm-yyyy or dd-mm
    ttxt = (text or "").strip()
    parts = ttxt.split("-")
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

def _lang_kb(*, update=None, context=None) -> ReplyKeyboardMarkup:
    rows = [[language_button_text(c)] for c in (available_languages() or ["ru", "en"])]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)

@dataclass
class StartHandler:
    users: UsersRepo

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        tg_user = update.effective_user
        chat_id = update.effective_chat.id if update.effective_chat else None

        u = await self.users.ensure_user(tg_user, chat_id=chat_id)
        if not u:
            await update.message.reply_text("oops, try again later.")
            return ConversationHandler.END

        # language first-time picker: show if no lang in user_data/chat_data
        if not context.user_data.get("lang") and not context.chat_data.get("lang"):
            # send zero-width space to keep chat clean (no visible text)
            await update.message.reply_text("\u200B", reply_markup=_lang_kb(update=update, context=context))
            return AWAITING_LANGUAGE

        # if birthday set, go to main
        has_bday = bool(u.get("birth_day") and u.get("birth_month"))
        if has_bday:
            await update.message.reply_text(t("main_menu_title", update=update, context=context), reply_markup=main_menu_kb(update=update, context=context))
            return ConversationHandler.END

        # ask for birthday in selected language
        await update.message.reply_text(t("start_bday_prompt", update=update, context=context))
        return AWAITING_REGISTRATION_BDAY

    async def language_chosen(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        choice = (update.message.text or "").strip()
        code = parse_language_choice(choice)
        if not code:
            # show picker again
            await update.message.reply_text("\u200B", reply_markup=_lang_kb(update=update, context=context))
            return AWAITING_LANGUAGE

        set_lang(code, context=context)

        # after set: ask for birthday if not set, else main
        uid = update.effective_user.id
        u = await self.users.get_user(uid)
        has_bday = bool(u and u.get("birth_day") and u.get("birth_month"))
        if has_bday:
            await update.message.reply_text(t("main_menu_title", update=update, context=context), reply_markup=main_menu_kb(update=update, context=context))
            return ConversationHandler.END

        await update.message.reply_text(t("start_bday_prompt", update=update, context=context))
        return AWAITING_REGISTRATION_BDAY

    async def reg_bday_entered(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "").strip()
        if text == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(t("canceled", update=update, context=context), reply_markup=main_menu_kb(update=update, context=context))
            return ConversationHandler.END

        parsed = _parse_bday(text)
        if not parsed:
            await update.message.reply_text(t("start_bday_bad", update=update, context=context))
            return AWAITING_REGISTRATION_BDAY

        d, m, y = parsed
        uid = update.effective_user.id

        try:
            # keep your repo api: update_bday (not set_birthday)
            await self.users.update_bday(uid, d, m, y)
        except Exception as e:
            log.exception("failed to set birthday: %s", e)
            await update.message.reply_text(t("start_bday_bad", update=update, context=context))
            return AWAITING_REGISTRATION_BDAY

        try:
            if update.effective_chat:
                await self.users.update_chat_id(uid, update.effective_chat.id)
        except Exception:
            pass

        await update.message.reply_text(
            t("start_bday_saved", update=update, context=context, d=f"{d:02d}", m=f"{m:02d}", y=(f"-{y}" if y else "")),
            reply_markup=main_menu_kb(update=update, context=context),
        )
        return ConversationHandler.END

# helper to wire conversation externally if needed
def conversation():
    return ConversationHandler(
        entry_points=[],
        states={
            AWAITING_LANGUAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, StartHandler.language_chosen)],
            AWAITING_REGISTRATION_BDAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, StartHandler.reg_bday_entered)],
        },
        fallbacks=[],
        name="conv_start_reg",
        persistent=False,
    )
