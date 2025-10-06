from __future__ import annotations

import logging
import re
import html
from typing import Optional, List, Dict

from telegram import Update, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from ..db.repo_wishlist import WishlistRepo
from ..db.repo_users import UsersRepo
from ..i18n import t, btn_regex

log = logging.getLogger("wishlist")

# states
W_EDIT_PICK = 0
W_ADD_TITLE = 1
W_ADD_URL = 2
W_ADD_PRICE = 3
W_DEL_ID = 4
W_VIEW_OTHER = 5


def _kb(rows):  # small helper
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def wishlist_menu_kb(*, update=None, context=None):
    return _kb([
        [t("btn_wishlist_my", update=update, context=context), t("btn_wishlist_edit", update=update, context=context)],
        [t("btn_wishlist_view", update=update, context=context)],
        [t("btn_back", update=update, context=context)],
    ])


def wishlist_edit_kb(*, update=None, context=None):
    return _kb([
        [t("btn_wishlist_add", update=update, context=context), t("btn_wishlist_del", update=update, context=context)],
        [t("btn_back", update=update, context=context)],
    ])


def cancel_kb(*, update=None, context=None):
    return _kb([[t("btn_cancel", update=update, context=context)]])


def back_cancel_kb(*, update=None, context=None):
    return _kb([[t("btn_back", update=update, context=context), t("btn_cancel", update=update, context=context)]])


def _parse_price_number(s: Optional[str]) -> float:
    """
    Try to extract a numeric value from a price string.
    - finds first number like 1,234.56 or 1 234,56 or 69, etc.
    - converts comma decimal to dot.
    Returns float value if found, else +inf to push to bottom when sorting.
    """
    if not s:
        return float("inf")
    txt = str(s)
    # remove currency symbols
    cleaned = re.sub(r"[^\d.,\s]", "", txt)
    # replace spaces as thousands separators
    cleaned = cleaned.replace(" ", "")
    # if there are both ',' and '.', assume ',' thousands and '.' decimal
    if "," in cleaned and "." in cleaned:
        # just drop commas
        cleaned = cleaned.replace(",", "")
    else:
        # if only comma present, treat as decimal
        cleaned = cleaned.replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", cleaned)
    if not m:
        return float("inf")
    try:
        return float(m.group(1))
    except Exception:
        return float("inf")


def _format_item_html(it: dict) -> str:
    """
    HTML-safe line:
    [n]. <a href="url">title</a> - price
    (the leading "[n]. " is added by caller; here we build link+price piece)
    """
    title = html.escape(it.get("title") or "—")
    url = (it.get("url") or "").strip()
    price = (it.get("price") or "").strip()

    if url:
        link = f'<a href="{html.escape(url, quote=True)}">{title}</a>'
    else:
        link = title  # no link available

    if price:
        return f"{link} - {html.escape(price)}"
    return link


def _sort_items_by_price(items: List[Dict]) -> List[Dict]:
    return sorted(items, key=lambda x: (_parse_price_number(x.get("price")), (x.get("title") or "").lower(), x.get("id") or 0))


class WishlistHandler:
    def __init__(self, wishlist: WishlistRepo, users: UsersRepo):
        self.wishlist = wishlist
        self.users = users

    # ------ Entry points (triggered via birthdays screen buttons) ------

    async def my_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        items = await self.wishlist.list_for_user(uid)
        if not items:
            await update.message.reply_text(
                t("wishlist_empty", update=update, context=context),
                reply_markup=wishlist_menu_kb(update=update, context=context),
            )
            return

        items_sorted = _sort_items_by_price(items)
        # Build mapping index -> db_id for deletion by short number
        id_map = [int(it["id"]) for it in items_sorted]
        context.user_data["__wl_map"] = id_map

        lines = [t("wishlist_header_my", update=update, context=context)]
        for i, it in enumerate(items_sorted, start=1):
            lines.append(f"{i}. {_format_item_html(it)}")
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=wishlist_menu_kb(update=update, context=context),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )

    async def edit_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            t("wishlist_edit_pick", update=update, context=context),
            reply_markup=wishlist_edit_kb(update=update, context=context),
        )
        return W_EDIT_PICK

    async def edit_pick(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "").strip()

        if text == t("btn_back", update=update, context=context):
            # Return to birthdays (outer menu will handle back)
            from .birthdays import BirthdaysHandler  # lazy import OK
            bh = context.application.bot_data.get("birthdays_handler")
            if bh:
                await bh.menu_entry(update, context)
            return ConversationHandler.END

        if text == t("btn_wishlist_add", update=update, context=context):
            await update.message.reply_text(
                t("wishlist_add_title", update=update, context=context),
                reply_markup=back_cancel_kb(update=update, context=context),
            )
            return W_ADD_TITLE

        if text == t("btn_wishlist_del", update=update, context=context):
            # show my list first, with local numbering
            uid = update.effective_user.id
            items = await self.wishlist.list_for_user(uid)
            if not items:
                await update.message.reply_text(
                    t("wishlist_empty", update=update, context=context),
                    reply_markup=wishlist_edit_kb(update=update, context=context),
                )
                return W_EDIT_PICK

            items_sorted = _sort_items_by_price(items)
            lines = [t("wishlist_header_my", update=update, context=context)]
            id_map = []
            for i, it in enumerate(items_sorted, start=1):
                id_map.append(int(it["id"]))
                lines.append(f"{i}. {_format_item_html(it)}")

            context.user_data["__wl_map"] = id_map
            lines.append("")
            lines.append(t("wishlist_del_prompt", update=update, context=context))
            await update.message.reply_text(
                "\n".join(lines),
                reply_markup=back_cancel_kb(update=update, context=context),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            return W_DEL_ID

        # unknown -> repeat menu
        await update.message.reply_text(
            t("wishlist_edit_pick", update=update, context=context),
            reply_markup=wishlist_edit_kb(update=update, context=context),
        )
        return W_EDIT_PICK

    # --- Add flow ---

    async def add_title(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "").strip()
        if text in (t("btn_back", update=update, context=context), t("btn_cancel", update=update, context=context)):
            await update.message.reply_text(
                t("canceled", update=update, context=context),
                reply_markup=wishlist_edit_kb(update=update, context=context),
            )
            return W_EDIT_PICK

        if not text:
            await update.message.reply_text(
                t("wishlist_add_title_bad", update=update, context=context),
                reply_markup=back_cancel_kb(update=update, context=context),
            )
            return W_ADD_TITLE

        context.user_data["__wl_new"] = {"title": text, "url": None, "price": None}
        await update.message.reply_text(
            t("wishlist_add_url", update=update, context=context),
            reply_markup=_kb([
                [t("btn_skip", update=update, context=context)],
                [t("btn_back", update=update, context=context), t("btn_cancel", update=update, context=context)],
            ]),
        )
        return W_ADD_URL

    async def add_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "").strip()
        if text == t("btn_back", update=update, context=context):
            await update.message.reply_text(
                t("wishlist_add_title", update=update, context=context),
                reply_markup=back_cancel_kb(update=update, context=context),
            )
            return W_ADD_TITLE
        if text == t("btn_cancel", update=update, context=context):
            context.user_data.pop("__wl_new", None)
            await update.message.reply_text(
                t("canceled", update=update, context=context),
                reply_markup=wishlist_edit_kb(update=update, context=context),
            )
            return W_EDIT_PICK
        if text != t("btn_skip", update=update, context=context):
            context.user_data.setdefault("__wl_new", {})["url"] = text

        await update.message.reply_text(
            t("wishlist_add_price", update=update, context=context),
            reply_markup=_kb([
                [t("btn_skip", update=update, context=context)],
                [t("btn_back", update=update, context=context), t("btn_cancel", update=update, context=context)],
            ]),
        )
        return W_ADD_PRICE

    async def add_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "").strip()
        if text == t("btn_back", update=update, context=context):
            await update.message.reply_text(
                t("wishlist_add_url", update=update, context=context),
                reply_markup=_kb([
                    [t("btn_skip", update=update, context=context)],
                    [t("btn_back", update=update, context=context), t("btn_cancel", update=update, context=context)],
                ]),
            )
            return W_ADD_URL
        if text == t("btn_cancel", update=update, context=context):
            context.user_data.pop("__wl_new", None)
            await update.message.reply_text(
                t("canceled", update=update, context=context),
                reply_markup=wishlist_edit_kb(update=update, context=context),
            )
            return W_EDIT_PICK
        if text != t("btn_skip", update=update, context=context):
            context.user_data.setdefault("__wl_new", {})["price"] = text

        # save
        uid = update.effective_user.id
        data = context.user_data.get("__wl_new") or {}
        title = data.get("title") or ""
        url = data.get("url")
        price = data.get("price")

        try:
            _ = await self.wishlist.add_item(uid, title=title, url=url, price=price)
            await update.message.reply_text(
                t("wishlist_add_ok", update=update, context=context),
                reply_markup=wishlist_edit_kb(update=update, context=context),
            )
        except Exception as e:
            log.exception("wishlist add failed: %s", e)
            await update.message.reply_text(
                t("wishlist_add_fail", update=update, context=context),
                reply_markup=wishlist_edit_kb(update=update, context=context),
            )

        context.user_data.pop("__wl_new", None)
        return W_EDIT_PICK

    # --- Delete flow ---

    async def del_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "").strip()
        if text == t("btn_back", update=update, context=context):
            await update.message.reply_text(
                t("wishlist_edit_pick", update=update, context=context),
                reply_markup=wishlist_edit_kb(update=update, context=context),
            )
            return W_EDIT_PICK
        if text == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(
                t("canceled", update=update, context=context),
                reply_markup=wishlist_edit_kb(update=update, context=context),
            )
            return W_EDIT_PICK

        # Accept either displayed local index (1..N) or real DB id
        wl_map: List[int] = context.user_data.get("__wl_map") or []
        target_db_id: Optional[int] = None

        if text.isdigit():
            num = int(text)
            # if matches local index 1..N -> map
            if 1 <= num <= len(wl_map):
                target_db_id = wl_map[num - 1]
            else:
                # maybe user typed real db id; accept as is
                target_db_id = num

        if not target_db_id:
            await update.message.reply_text(
                t("wishlist_del_bad", update=update, context=context),
                reply_markup=back_cancel_kb(update=update, context=context),
            )
            return W_DEL_ID

        uid = update.effective_user.id
        ok = False
        try:
            ok = await self.wishlist.delete_item(uid, target_db_id)
        except Exception:
            ok = False

        await update.message.reply_text(
            t("wishlist_del_ok", update=update, context=context) if ok else t("wishlist_del_fail", update=update, context=context),
            reply_markup=wishlist_edit_kb(update=update, context=context),
        )
        return W_EDIT_PICK

    # --- View other's wishlist ---

    async def view_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            t("wishlist_view_prompt", update=update, context=context),
            reply_markup=back_cancel_kb(update=update, context=context),
        )
        return W_VIEW_OTHER

    async def view_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "").strip()
        if text == t("btn_back", update=update, context=context):
            await update.message.reply_text(
                t("wishlist_open_menu", update=update, context=context),
                reply_markup=wishlist_menu_kb(update=update, context=context),
            )
            return ConversationHandler.END
        if text == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(
                t("canceled", update=update, context=context),
                reply_markup=wishlist_menu_kb(update=update, context=context),
            )
            return ConversationHandler.END

        # parse @username or id
        target_id: Optional[int] = None
        username: Optional[str] = None
        if text.startswith("@"):
            username = text[1:]
        elif text.isdigit():
            try:
                target_id = int(text)
            except Exception:
                target_id = None

        # resolve user id if username given
        if username and not target_id:
            up = await self.users.get_user_by_username(username)
            if up:
                target_id = int(up.get("user_id"))

        if not target_id:
            await update.message.reply_text(
                t("wishlist_view_not_found", update=update, context=context),
                reply_markup=back_cancel_kb(update=update, context=context),
            )
            return W_VIEW_OTHER

        items = await self.wishlist.list_for_user(target_id)
        if not items:
            await update.message.reply_text(
                t("wishlist_empty_other", update=update, context=context),
                reply_markup=wishlist_menu_kb(update=update, context=context),
            )
            return ConversationHandler.END

        items_sorted = _sort_items_by_price(items)
        lines = [t("wishlist_header_other", update=update, context=context)]
        for i, it in enumerate(items_sorted, start=1):
            lines.append(f"{i}. {_format_item_html(it)}")
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=wishlist_menu_kb(update=update, context=context),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        return ConversationHandler.END

    # wiring
    def conversation_handlers(self):
        return [
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(btn_regex("btn_wishlist_edit")), self.edit_start)],
                states={
                    W_EDIT_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_pick)],
                    W_ADD_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_title)],
                    W_ADD_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_url)],
                    W_ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_price)],
                    W_DEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.del_id)],
                },
                fallbacks=[MessageHandler(filters.Regex(btn_regex("btn_cancel")), self.edit_start)],
                name="conv_wishlist_edit",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(btn_regex("btn_wishlist_view")), self.view_start)],
                states={W_VIEW_OTHER: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.view_wait)]},
                fallbacks=[MessageHandler(filters.Regex(btn_regex("btn_cancel")), self.view_start)],
                name="conv_wishlist_view",
                persistent=False,
            ),
        ]
