from __future__ import annotations

import logging
from typing import Optional

from telegram import Update, LabeledPrice
from telegram.ext import ContextTypes

from ..keyboards import about_kb
from ..i18n import t

class AboutHandler:
    def __init__(self) -> None:
        self.log = logging.getLogger("about")

    async def menu_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = t("about_text", update=update, context=context)
        await update.message.reply_text(text, reply_markup=about_kb(update=update, context=context))

    # donate buttons route here
    async def donate_50(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_invoice(update, context, stars=50)

    async def donate_100(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_invoice(update, context, stars=100)

    async def donate_500(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_invoice(update, context, stars=500)

    async def _send_invoice(self, update: Update, context: ContextTypes.DEFAULT_TYPE, stars: int):
        # telegram stars: currency XTR, amount is stars
        chat_id = update.effective_chat.id
        title = t("donate_title", update=update, context=context, stars=stars)
        desc = t("donate_desc", update=update, context=context)
        payload = f"donation:{stars}"
        await context.bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description=desc,
            payload=payload,
            provider_token="",  # stars mode, no external provider
            currency="XTR",
            prices=[LabeledPrice(label=f"{stars} stars", amount=stars)],
            is_flexible=False,
        )

    async def precheckout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            await update.pre_checkout_query.answer(ok=True)
        except Exception as e:
            self.log.exception("precheckout failed: %s", e)

    async def successful_payment(self, update: Update, _context: ContextTypes.DEFAULT_TYPE):
        try:
            sp = update.message.successful_payment
            amount = sp.total_amount  # for XTR this equals stars
            await update.message.reply_text(t("donate_thanks", update=update, context=_context, amount=amount))
        except Exception as e:
            self.log.exception("successful_payment handler failed: %s", e)
