from __future__ import annotations

import logging
from typing import Optional

from telegram import Update, LabeledPrice
from telegram.ext import ContextTypes

from ..keyboards import about_kb

# note: comments are lowercase and short

class AboutHandler:
    def __init__(self) -> None:
        self.log = logging.getLogger("about")

    async def menu_entry(self, update: Update, _context: ContextTypes.DEFAULT_TYPE):
        # simple about text with links
        text = (
            "мы опенсорс.\n"
            "github: https://github.com/smmac72/birthdaybot\n"
            "issues: https://github.com/smmac72/birthdaybot/issues\n\n"
            "этот бот напоминает о днях рождения: друзья, группы, часовые пояса и заранее уведомления."
        )
        await update.message.reply_text(text, reply_markup=about_kb())

    # donate buttons route here
    async def donate_50(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_invoice(update, context, stars=50)

    async def donate_100(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_invoice(update, context, stars=100)

    async def donate_500(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_invoice(update, context, stars=500)

    async def _send_invoice(self, update: Update, context: ContextTypes.DEFAULT_TYPE, stars: int):
        # send invoice in telegram stars. currency is XTR, amount is stars
        chat_id = update.effective_chat.id
        title = f"поддержать проект ({stars}⭐)"
        desc = "донат на развитие birthdaybot"
        payload = f"donation:{stars}"
        # provider_token is intentionally empty for stars
        await context.bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description=desc,
            payload=payload,
            provider_token="", # stars mode, no external provider
            currency="XTR",
            prices=[LabeledPrice(label=f"{stars} stars", amount=stars)],
            is_flexible=False,
        )

    # pre-checkout confirm
    async def precheckout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            await update.pre_checkout_query.answer(ok=True)
        except Exception as e:
            self.log.exception("precheckout failed: %s", e)

    # successful payment ack
    async def successful_payment(self, update: Update, _context: ContextTypes.DEFAULT_TYPE):
        try:
            sp = update.message.successful_payment
            amount = sp.total_amount  # for XTR this equals stars
            await update.message.reply_text(f"спасибо за поддержку! получено {amount}⭐")
        except Exception as e:
            self.log.exception("successful_payment handler failed: %s", e)
