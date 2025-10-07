from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple, List
from telegram import ParseMode
from .. import config

import datetime as dt
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CommandHandler,
    filters,
)

from ..db.repo_users import UsersRepo
from ..keyboards import main_menu_kb
from ..i18n import (
    t,
    available_languages,
    language_button_text,
    parse_language_choice,
    set_lang,
    current_lang,
    language_label,
)

log = logging.getLogger("start")

# states
AWAITING_REGISTRATION_BDAY = 1
AWAITING_LANG_PICK = 2


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
    try:
        _ = dt.date(y if y else 2000, m, d)
    except ValueError:
        if not (m == 2 and d == 29):
            return None
    return d, m, y


def _lang_kb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> ReplyKeyboardMarkup:
    # build rows of language buttons
    codes: List[str] = available_languages() or ["ru", "en"]
    rows = [[language_button_text(c)] for c in codes]
    rows.append([t("btn_cancel", update=update, context=context)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


@dataclass
class StartHandler:
    users: UsersRepo

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # ensure user row and chat id
        tg_user = update.effective_user
        chat_id = update.effective_chat.id if update.effective_chat else None

        u = await self.users.ensure_user(tg_user, chat_id=chat_id)
        if not u:
            await update.message.reply_text(t("not_found", update=update, context=context))
            return ConversationHandler.END

        # first-time language pick if no tz/lang in context (we store lang in context.user_data["lang"])
        lang_code = context.user_data.get("lang")
        if not lang_code:
            cur = current_lang(update=update, context=context)
            lbl = language_label(cur)
            await update.message.reply_text(
                t("settings_lang_pick", update=update, context=context, lang=lbl),
                reply_markup=_lang_kb(update, context),
            )
            return AWAITING_LANG_PICK

        # if birthday is already set - straight to main menu
        has_bday = bool(u.get("birth_day") and u.get("birth_month"))
        if has_bday:
            await update.message.reply_text(
                t("start_back", update=update, context=context),
                reply_markup=main_menu_kb(update=update, context=context),
            )
            return ConversationHandler.END

        # ask for birthday with a clean prompt
        await update.message.reply_text(
            t("start_bday_prompt", update=update, context=context),
            reply_markup=ReplyKeyboardMarkup(
                [[t("btn_cancel", update=update, context=context)]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return AWAITING_REGISTRATION_BDAY

    async def reg_bday_entered(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # handle birthday input during registration
        text = (update.message.text or "").strip()
        if text == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(
                t("canceled", update=update, context=context),
                reply_markup=main_menu_kb(update=update, context=context),
            )
            return ConversationHandler.END

        parsed = _parse_bday(text)
        if not parsed:
            await update.message.reply_text(
                t("start_bday_bad", update=update, context=context)
            )
            return AWAITING_REGISTRATION_BDAY

        d, m, y = parsed

        if y is not None:
            try:
                dt.date(y, m, d)
            except ValueError:
                if m == 2 and d == 29:
                    y = None
                else:
                    await update.message.reply_text(
                        t("start_bday_bad", update=update, context=context)
                    )
                    return AWAITING_REGISTRATION_BDAY

        uid = update.effective_user.id

        # save birthday
        try:
            await self.users.update_bday(uid, d, m, y)
        except Exception as e:
            log.exception("failed to set birthday: %s", e)
            await update.message.reply_text(
                t("start_bday_bad", update=update, context=context)
            )
            return AWAITING_REGISTRATION_BDAY

        # re-save chat id for safety
        try:
            if update.effective_chat:
                await self.users.update_chat_id(uid, update.effective_chat.id)
        except Exception:
            pass

        notif = context.application.bot_data.get("notif_service")
        if notif:
            try:
                await notif.reschedule_for_person(uid, update.effective_user.username)
            except Exception as e:
                log.exception("reschedule after registration birthday failed: %s", e)

        await update.message.reply_text(
            t(
                "start_bday_saved",
                update=update,
                context=context,
                d=f"{d:02d}",
                m=f"{m:02d}",
                y=(f"-{y}" if y else ""),
            ),
            reply_markup=main_menu_kb(update=update, context=context),
        )

        ## alert about registration
        try:
            if config.ADMIN_CHAT_ID:
                user = update.effective_user
                msg = (
                    f"👤 <b>Новый пользователь зарегистрировался</b>\n"
                    f"ID: <code>{user.id}</code>\n"
                    f"Имя: {user.full_name}\n"
                    f"Язык: {current_lang(update=update, context=context)}\n"
                    f"Дата рождения: {d:02d}-{m:02d}{('-' + str(y)) if y else ''}"
                )
                await context.bot.send_message(
                    chat_id=config.ADMIN_CHAT_ID,
                    text=msg,
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            log.warning("failed to notify admin about registration: %s", e)
        return ConversationHandler.END

    async def lang_pick_entered(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "").strip()
        if text == t("btn_cancel", update=update, context=context):
            # if canceled, still show main menu with current lang
            await update.message.reply_text(
                t("main_menu_title", update=update, context=context),
                reply_markup=main_menu_kb(update=update, context=context),
            )
            return ConversationHandler.END

        code = parse_language_choice(text)
        if not code:
            await update.message.reply_text(
                t("settings_lang_bad", update=update, context=context),
                reply_markup=_lang_kb(update, context),
            )
            return AWAITING_LANG_PICK

        # persist in context
        set_lang(code, context=context)

        picked_lbl = language_label(code)
        await update.message.reply_text(
            t("settings_lang_set_ok", update=update, context=context, lang=picked_lbl),
            reply_markup=main_menu_kb(update=update, context=context),
        )

        # proceed to next step: if user has bday — main; else ask bday
        uid = update.effective_user.id
        u = await self.users.get_user(uid)
        has_bday = bool(u and u.get("birth_day") and u.get("birth_month"))
        if has_bday:
            await update.message.reply_text(
                t("start_back", update=update, context=context),
                reply_markup=main_menu_kb(update=update, context=context),
            )
            return ConversationHandler.END

        await update.message.reply_text(
            t("start_bday_prompt", update=update, context=context),
            reply_markup=ReplyKeyboardMarkup(
                [[t("btn_cancel", update=update, context=context)]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return AWAITING_REGISTRATION_BDAY


def conversation(start_handler: StartHandler) -> ConversationHandler:
    return ConversationHandler(
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
        name="conv_start",
        persistent=False,
    )
