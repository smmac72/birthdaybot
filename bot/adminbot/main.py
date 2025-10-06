# admin bot with compose-and-send and maintenance soft/hard
from __future__ import annotations

import asyncio
import io
import logging
from typing import Dict, List, Tuple, Optional

from telegram import Update, Bot, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputFile
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters, JobQueue
)

from .. import config
from .repo import AdminRepo

log = logging.getLogger("adminbot")

PENDING_KEY = "pending_send"   # {'mode': 'broadcast'|'maint_on_soft'|'maint_on_hard'|'maint_off'}
ALBUM_BUF = "album_buffer"
ALBUM_JOB = "album_job"

def _setup_logging():
    level = getattr(logging, (config.LOG_LEVEL or "INFO").upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)

def _is_admin(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return bool(uid and uid in (getattr(config, "ADMIN_ALLOWED_IDS", []) or []))

async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if _is_admin(update):
        return True
    await update.effective_message.reply_text("not allowed.")
    return False

def get_main_bot() -> Bot:
    token = (config.BOT_TOKEN or "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is empty")
    return Bot(token=token)

async def _list_all_chats(repo: AdminRepo) -> List[int]:
    await repo.ensure_schema()
    return await repo.list_all_chat_ids()

# ---------------- media helpers (download via admin, upload via main)

async def _download_bytes(ctx: ContextTypes.DEFAULT_TYPE, file_id: str) -> InputFile:
    f = await ctx.bot.get_file(file_id)
    buf = io.BytesIO()
    await f.download_to_memory(out=buf)
    buf.seek(0)
    name = getattr(f, "file_path", None)
    name = name.split("/")[-1] if isinstance(name, str) and name else "file.bin"
    return InputFile(buf, filename=name)

async def _single_to_send(ctx: ContextTypes.DEFAULT_TYPE, msg) -> Tuple[str, dict]:
    if msg.text:
        return "send_message", {"text": msg.text}
    if msg.photo:
        return "send_photo", {"photo": await _download_bytes(ctx, msg.photo[-1].file_id), "caption": msg.caption or None}
    if msg.video:
        return "send_video", {"video": await _download_bytes(ctx, msg.video.file_id), "caption": msg.caption or None}
    if msg.document:
        return "send_document", {"document": await _download_bytes(ctx, msg.document.file_id), "caption": msg.caption or None}
    if msg.voice:
        return "send_voice", {"voice": await _download_bytes(ctx, msg.voice.file_id), "caption": msg.caption or None}
    if msg.audio:
        return "send_audio", {"audio": await _download_bytes(ctx, msg.audio.file_id), "caption": msg.caption or None}
    return "send_message", {"text": "unsupported content type for broadcast."}

async def _album_items(ctx: ContextTypes.DEFAULT_TYPE, msgs: List) -> List:
    out = []
    for i, m in enumerate(msgs):
        cap = m.caption if i == 0 else None
        if m.photo:
            out.append(InputMediaPhoto(media=await _download_bytes(ctx, m.photo[-1].file_id), caption=cap))
        elif m.video:
            out.append(InputMediaVideo(media=await _download_bytes(ctx, m.video.file_id), caption=cap))
        elif m.document:
            out.append(InputMediaDocument(media=await _download_bytes(ctx, m.document.file_id), caption=cap))
    return out

# ---------------- fanout

async def _fanout_single(main_bot: Bot, method: str, kwargs: dict, chat_ids: List[int], rate: int = 20) -> Tuple[int,int]:
    sent = failed = bucket = 0
    for cid in chat_ids:
        try:
            await getattr(main_bot, method)(chat_id=cid, **kwargs)
            sent += 1
            bucket += 1
            if bucket >= rate:
                bucket = 0
                await asyncio.sleep(1.0)
        except Exception:
            failed += 1
    return sent, failed

async def _fanout_album(main_bot: Bot, media: List, chat_ids: List[int], rate: int = 8) -> Tuple[int,int]:
    sent = failed = bucket = 0
    if not media:
        return 0, len(chat_ids)
    for cid in chat_ids:
        try:
            await main_bot.send_media_group(chat_id=cid, media=media)
            sent += 1
            bucket += 1
            if bucket >= rate:
                bucket = 0
                await asyncio.sleep(1.0)
        except Exception:
            failed += 1
    return sent, failed

# ---------------- commands

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context): return
    await update.message.reply_text(
        "/help — this help\n"
        "/ping — liveness\n"
        "/stats — summary stats\n"
        "/top_groups — top 10 groups by members\n"
        "/top_users — top 10 users by followers\n"
        "/errors [N] — recent errors\n"
        "/admin_reset — drop & recreate admin tables (danger!)\n\n"
        "/broadcast — compose-and-send to all users\n"
        "/maintenance_on [soft|hard] — enable maintenance and notify users\n"
        "/maintenance_off — disable maintenance and notify users\n"
        "/say <user_id> <text> — DM via main bot\n"
    )

async def cmd_admin_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context): return
    repo = AdminRepo(config.DB_PATH)
    await repo.reset_schema()
    await update.message.reply_text("admin schema reset ok.")

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context): return
    await update.message.reply_text("pong")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context): return
    repo = AdminRepo(config.DB_PATH); await repo.ensure_schema(); s = await repo.stats_summary()
    await update.message.reply_text(
        "stats:\n"
        f"• users: {s.get('users_total',0)}\n"
        f"• users w/ birthday: {s.get('users_with_bday',0)}\n"
        f"• groups: {s.get('groups_total',0)}\n"
        f"• friends links: {s.get('friends_total',0)}\n"
        f"• notifications (30d): {s.get('notif_30d',0)}\n"
    )

async def cmd_top_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context): return
    repo = AdminRepo(config.DB_PATH); await repo.ensure_schema(); rows = await repo.top_groups()
    if not rows: await update.message.reply_text("no groups."); return
    lines = ["top groups:"] + [f"{i}. {r['name']} (code: {r['code']}) — {int(r['members'] or 0)}" for i,r in enumerate(rows,1)]
    await update.message.reply_text("\n".join(lines))

async def cmd_top_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context): return
    repo = AdminRepo(config.DB_PATH); await repo.ensure_schema(); rows = await repo.top_users_followed()
    if not rows: await update.message.reply_text("no users."); return
    lines = ["top users by followers:"] + [
        (f"{i}. @{r['username']}" if r.get("username") else f"{i}. id:{r['user_id']}")
        for i,r in enumerate(rows,1)
    ]
    await update.message.reply_text("\n".join(lines))

async def cmd_errors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context): return
    n = 20
    if context.args:
        try: n = max(1, min(100, int(context.args[0])))
        except Exception: n = 20
    repo = AdminRepo(config.DB_PATH); await repo.ensure_schema()
    rows = await repo.errors_recent(n)
    if not rows: await update.message.reply_text("no recent errors."); return
    lines = ["recent errors:"] + [f"[{r['id']}] {r['ts']} {r['level']} {r['source']} — {r['message']}" for r in rows]
    await update.message.reply_text("\n".join(lines))

async def cmd_say(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context): return
    if len(context.args or []) < 2:
        await update.message.reply_text("usage: /say <user_id> <text>"); return
    try: uid = int(context.args[0])
    except Exception: await update.message.reply_text("bad user_id"); return
    text = " ".join(context.args[1:]).strip()
    if not text: await update.message.reply_text("empty text"); return
    try:
        await get_main_bot().send_message(chat_id=uid, text=text)
        await update.message.reply_text("ok")
    except Exception as e:
        await update.message.reply_text(f"send failed: {e!s}")

# ---- compose-and-send entry

async def _enter_compose(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    if not await _guard(update, context): return
    context.user_data[PENDING_KEY] = {"mode": mode}
    await update.message.reply_text("ok, send the message now (text/photo/video/document or album).")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _enter_compose(update, context, "broadcast")

async def cmd_maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context): return
    mode = "soft"
    if context.args and context.args[0].lower() in ("soft","hard"):
        mode = context.args[0].lower()
    repo = AdminRepo(config.DB_PATH); await repo.ensure_schema()
    # flip flag first
    await repo.set_maintenance(enabled=True, mode=mode)
    # enqueue key so main bot tells users in their language
    key = "maintenance_on_soft" if mode == "soft" else "maintenance_on_hard"
    await repo.enqueue_event("maint", {"key": key})
    #await _enter_compose(update, context, f"maint_on_{mode}")

async def cmd_maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context): return
    repo = AdminRepo(config.DB_PATH); await repo.ensure_schema()
    await repo.set_maintenance(enabled=False, mode="soft")
    await repo.enqueue_event("maint", {"key": "maintenance_off"})
    #await _enter_compose(update, context, "maint_off")

# ---- intake payload

def _album_buffers(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, List]:
    buf = ctx.chat_data.get(ALBUM_BUF)
    if buf is None:
        buf = {}
        ctx.chat_data[ALBUM_BUF] = buf
    return buf

async def _flush_album(ctx: ContextTypes.DEFAULT_TYPE, media_group_id: str, admin_chat_id: int, mode: str):
    repo = AdminRepo(config.DB_PATH); await repo.ensure_schema()
    chat_ids = await _list_all_chats(repo)
    main_bot = get_main_bot()
    msgs = _album_buffers(ctx).pop(media_group_id, [])
    media = await _album_items(ctx, msgs)
    sent, failed = await _fanout_album(main_bot, media, chat_ids)
    try: await ctx.bot.send_message(chat_id=admin_chat_id, text=f"sent: {sent}, failed: {failed}")
    except Exception: pass
    ctx.chat_data.pop(ALBUM_JOB, None)
    ctx.user_data.pop(PENDING_KEY, None)

async def _schedule_album_flush(ctx: ContextTypes.DEFAULT_TYPE, mgid: str, admin_chat_id: int, mode: str):
    jq: JobQueue = ctx.application.job_queue  # type: ignore
    old = ctx.chat_data.get(ALBUM_JOB)
    if old:
        try: old.schedule_removal()
        except Exception: pass
    ctx.chat_data[ALBUM_JOB] = jq.run_once(lambda _job_ctx: asyncio.create_task(_flush_album(ctx, mgid, admin_chat_id, mode)), when=1.0)

async def intake_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return
    pend = context.user_data.get(PENDING_KEY)
    if not pend: return
    mode = pend.get("mode") or "broadcast"
    msg = update.message
    if not msg: return

    repo = AdminRepo(config.DB_PATH); await repo.ensure_schema()
    main_bot = get_main_bot()

    # albums
    if msg.media_group_id:
        mgid = str(msg.media_group_id)
        _album_buffers(context).setdefault(mgid, []).append(msg)
        await _schedule_album_flush(context, mgid, msg.chat_id, mode)
        return

    chat_ids = await _list_all_chats(repo)
    method, kwargs = await _single_to_send(context, msg)
    sent, failed = await _fanout_single(main_bot, method, kwargs, chat_ids)
    try: await update.message.reply_text(f"done. sent: {sent}, failed: {failed}")
    except Exception: pass
    context.user_data.pop(PENDING_KEY, None)

# ---- app

def build_application() -> Application:
    _setup_logging()
    if not (getattr(config, "ADMIN_BOT_TOKEN", "") or "").strip():
        raise RuntimeError("ADMIN_BOT_TOKEN is empty")
    app = ApplicationBuilder().token(config.ADMIN_BOT_TOKEN).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("admin_reset", cmd_admin_reset))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("top_groups", cmd_top_groups))
    app.add_handler(CommandHandler("top_users", cmd_top_users))
    app.add_handler(CommandHandler("errors", cmd_errors))
    app.add_handler(CommandHandler("say", cmd_say))

    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("maintenance_on", cmd_maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", cmd_maintenance_off))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, intake_message))
    return app

def main():
    app = build_application()
    app.run_polling()

if __name__ == "__main__":
    main()
