from __future__ import annotations
import logging
import re
import uuid
from typing import Optional, List, Dict, Any

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from ..db.repo_groups import GroupsRepo
from ..db.repo_users import UsersRepo
from ..keyboards import groups_menu_kb, group_mgmt_kb

# states
STATE_WAIT_GROUP_NAME = 0
STATE_WAIT_JOIN_CODE = 1
STATE_WAIT_LEAVE_CODE = 2
STATE_WAIT_RENAME = 3
STATE_WAIT_ADD_MEMBER = 4
STATE_WAIT_DEL_MEMBER = 5

def _log_id() -> str:
    return uuid.uuid4().hex[:8]

def _cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["◀️ отмена"]], resize_keyboard=True, one_time_keyboard=True)

def _icon_registered(user_id: Optional[int]) -> str:
    return "✅" if user_id else "⚪️"

def _fmt_bday(d, m, y) -> str:
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

def _member_line(m: Dict[str, Any]) -> str:
    icon = _icon_registered(m.get("user_id"))
    name = f"@{m['username']}" if m.get("username") else (f"id:{m['user_id']}" if m.get("user_id") else "unknown")
    bd = _fmt_bday(m.get("birth_day"), m.get("birth_month"), m.get("birth_year"))
    dleft = _days_until_key(m.get("birth_day"), m.get("birth_month"))
    when = _when_str(dleft)
    return f"• {icon} {name} — {bd} ({when})"

class GroupsHandler:
    def __init__(self, groups: GroupsRepo, users: UsersRepo):
        self.groups = groups
        self.users = users
        self.log = logging.getLogger("groups")

    async def menu_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        rid = _log_id()
        uid = update.effective_user.id
        self.log.info("[%s] menu_entry: user_id=%s", rid, uid)

        try:
            rows = await self.groups.list_user_groups(uid)
            cnt = len(rows)
            self.log.info("[%s] menu_entry: groups_count=%s", rid, cnt)
        except Exception as e:
            self.log.exception("[%s] menu_entry failed: %s", rid, e)
            await update.message.reply_text("не удалось получить список групп.", reply_markup=groups_menu_kb())
            return

        if not rows:
            await update.message.reply_text("у вас нет групп.", reply_markup=groups_menu_kb())
            return

        lines = ["ваши группы:\n"]
        for g in rows:
            g = dict(g)
            mark = " 👑 вы создатель" if g.get("creator_user_id") == uid else ""
            lines.append(f"📌 {g['name']} (код: {g['code']}) — {int(g.get('member_count', 0))} участников{mark}")

        await update.message.reply_text("\n\n".join(["\n".join(lines), "выберите действие:"]), reply_markup=groups_menu_kb())

    async def manage_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        try:
            rows = await self.groups.list_user_groups(uid)
        except Exception:
            rows = []
        managed = [dict(r) for r in rows if int(r.get("creator_user_id", 0)) == uid]

        if not managed:
            await update.message.reply_text("у вас нет групп под управлением.", reply_markup=groups_menu_kb())
            return

        kb = ReplyKeyboardMarkup(
            [[f"🛠 {g['name']} ({g['code']})"] for g in managed] + [["⬅️ выйти"]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text("выберите группу для управления:", reply_markup=kb)

    async def _render_group_members(self, update: Update, gid: str) -> List[Dict[str, Any]]:
        members = await self.groups.list_members(gid)
        members = [dict(m) for m in members]
        members.sort(key=lambda m: _days_until_key(m.get("birth_day"), m.get("birth_month")))
        lines = [f"участники ({len(members)}):"]
        for m in members:
            lines.append(_member_line(m))
        await update.message.reply_text("\n".join(lines))
        return members

    # create
    async def create_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("введите название новой группы:", reply_markup=_cancel_kb())
        return STATE_WAIT_GROUP_NAME

    async def create_wait_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "").strip()
        if text == "◀️ отмена":
            await update.message.reply_text("отменено.", reply_markup=groups_menu_kb())
            return ConversationHandler.END
        name = text
        gid, code = await self.groups.create_group(name, update.effective_user.id)
        await update.message.reply_text(f"группа '{name}' создана.\nкод приглашения: {code}", reply_markup=groups_menu_kb())

        # reschedule for creator (their followers might need it)
        notif = context.application.bot_data.get("notif_service")
        if notif:
            try:
                await notif.reschedule_for_person(update.effective_user.id, update.effective_user.username)
            except Exception as e:
                self.log.exception("reschedule after create group failed: %s", e)

        return ConversationHandler.END

    # join
    async def join_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("введите код группы:", reply_markup=_cancel_kb())
        return STATE_WAIT_JOIN_CODE

    async def join_wait_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        code = (update.message.text or "").strip()
        if code == "◀️ отмена":
            await update.message.reply_text("отменено.", reply_markup=groups_menu_kb())
            return ConversationHandler.END
        ok, name = await self.groups.join_by_code(code, update.effective_user.id)
        if ok:
            await update.message.reply_text(f"вы присоединились к группе '{name}'.", reply_markup=groups_menu_kb())
            notif = context.application.bot_data.get("notif_service")
            if notif:
                try:
                    await notif.reschedule_for_person(update.effective_user.id, update.effective_user.username)
                except Exception as e:
                    self.log.exception("reschedule after join failed: %s", e)
        else:
            await update.message.reply_text("неверный код или вы уже в группе.", reply_markup=groups_menu_kb())
        return ConversationHandler.END

    # leave
    async def leave_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("введите код группы для выхода:", reply_markup=_cancel_kb())
        return STATE_WAIT_LEAVE_CODE

    async def leave_wait_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        code = (update.message.text or "").strip()
        if code == "◀️ отмена":
            await update.message.reply_text("отменено.", reply_markup=groups_menu_kb())
            return ConversationHandler.END
        ok, name = await self.groups.leave_by_code(code, update.effective_user.id)
        if ok:
            await update.message.reply_text(f"вы покинули группу '{name}'.", reply_markup=groups_menu_kb())
            notif = context.application.bot_data.get("notif_service")
            if notif:
                try:
                    await notif.reschedule_for_person(update.effective_user.id, update.effective_user.username)
                except Exception as e:
                    self.log.exception("reschedule after leave failed: %s", e)
        else:
            await update.message.reply_text("неверный код группы или вы не состоите в ней.", reply_markup=groups_menu_kb())
        return ConversationHandler.END

    # manage entry
    async def manage_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "")
        m = re.match(r"^🛠\s+(.+)\s+\(([\w-]+)\)$", text)
        if not m:
            await update.message.reply_text("выберите группу из меню.", reply_markup=groups_menu_kb())
            return ConversationHandler.END
        code = m.group(2)
        g = await self.groups.get_by_code(code)
        if not g:
            await update.message.reply_text("группа не найдена.", reply_markup=groups_menu_kb())
            return ConversationHandler.END

        gid = g["group_id"]
        context.user_data["mgmt_gid"] = gid

        await update.message.reply_text(f"группа: {g['name']}")
        await self._render_group_members(update, gid)
        await update.message.reply_text("выберите действие:", reply_markup=group_mgmt_kb())
        return ConversationHandler.END

    # rename
    async def rename_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        gid = context.user_data.get("mgmt_gid")
        if not gid:
            await update.message.reply_text("сначала выберите группу в управлении.", reply_markup=groups_menu_kb())
            return ConversationHandler.END
        await update.message.reply_text("введите новое имя группы:", reply_markup=_cancel_kb())
        return STATE_WAIT_RENAME

    async def rename_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        gid = context.user_data.get("mgmt_gid")
        if not gid:
            await update.message.reply_text("сначала выберите группу в управлении.", reply_markup=groups_menu_kb())
            return ConversationHandler.END
        text = (update.message.text or "").strip()
        if text == "◀️ отмена":
            await update.message.reply_text("отменено.", reply_markup=group_mgmt_kb())
            return ConversationHandler.END
        await self.groups.rename_group(gid, text)
        await update.message.reply_text("имя обновлено.")
        await self._render_group_members(update, gid)
        await update.message.reply_text("выберите действие:", reply_markup=group_mgmt_kb())
        return ConversationHandler.END

    # add member
    async def add_member_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        gid = context.user_data.get("mgmt_gid")
        if not gid:
            await update.message.reply_text("сначала выберите группу в управлении.", reply_markup=groups_menu_kb())
            return ConversationHandler.END
        await update.message.reply_text(
            "введите @username или id. можно с датой: @user дд-мм(-гггг).\nесли пользователя нет в боте — дата обязательна.",
            reply_markup=_cancel_kb(),
        )
        return STATE_WAIT_ADD_MEMBER

    async def add_member_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        gid = context.user_data.get("mgmt_gid")
        if not gid:
            await update.message.reply_text("сначала выберите группу в управлении.", reply_markup=groups_menu_kb())
            return ConversationHandler.END

        text = (update.message.text or "").strip()
        if text == "◀️ отмена":
            await update.message.reply_text("отменено.", reply_markup=group_mgmt_kb())
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

        m = re.search(r"\b(\d{2})-(\d{2})(?:-(\d{4}))?\b", text)
        bd = None
        if m:
            d, mo = int(m.group(1)), int(m.group(2))
            y = int(m.group(3)) if m.group(3) else None
            if _valid_date(d, mo, y):
                bd = (d, mo, y)

        # resolve registered profile if possible
        prof = None
        if user_id:
            prof = await self.users.get_user(user_id)
        elif username:
            prof = await self.users.get_user_by_username(username)
        prof = dict(prof) if prof else None

        notif = context.application.bot_data.get("notif_service")

        if prof:
            await self.groups.add_member(
                gid, prof.get("user_id"), prof.get("username"),
                prof.get("birth_day"), prof.get("birth_month"), prof.get("birth_year"),
            )
            # reschedule: target person only
            if notif:
                try:
                    await notif.reschedule_for_person(prof.get("user_id"), prof.get("username"))
                except Exception as e:
                    self.log.exception("reschedule add member failed: %s", e)
            await update.message.reply_text("участник добавлен.")
        else:
            if not bd:
                await update.message.reply_text("этого пользователя нет в боте. укажите дату как дд-мм(-гггг).", reply_markup=_cancel_kb())
                return STATE_WAIT_ADD_MEMBER
            d, mo, y = bd
            await self.groups.add_member(gid, user_id, username, d, mo, y)
            await update.message.reply_text("участник добавлен.")

        await self._render_group_members(update, gid)
        await update.message.reply_text("выберите действие:", reply_markup=group_mgmt_kb())
        return ConversationHandler.END

    # delete member
    async def del_member_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        gid = context.user_data.get("mgmt_gid")
        if not gid:
            await update.message.reply_text("сначала выберите группу в управлении.", reply_markup=groups_menu_kb())
            return ConversationHandler.END
        await update.message.reply_text("введите @username или id участника для удаления:", reply_markup=_cancel_kb())
        return STATE_WAIT_DEL_MEMBER

    async def del_member_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        gid = context.user_data.get("mgmt_gid")
        if not gid:
            await update.message.reply_text("сначала выберите группу в управлении.", reply_markup=groups_menu_kk())
            return ConversationHandler.END

        text = (update.message.text or "").strip()
        if text == "◀️ отмена":
            await update.message.reply_text("отменено.", reply_markup=group_mgmt_kb())
            return ConversationHandler.END

        if text.isdigit():
            target_id = int(text)
            target_un = None
        elif text.startswith("@"):
            target_id = None
            target_un = text[1:]
        else:
            await update.message.reply_text("укажите @username или id")
            return STATE_WAIT_DEL_MEMBER

        # do not allow kicking self
        if target_id and target_id == update.effective_user.id:
            await update.message.reply_text("нельзя удалить себя. используйте '🚪 покинуть группу'.", reply_markup=group_mgmt_kb())
            return ConversationHandler.END

        ok = False
        try:
            ok = await self.groups.remove_member(gid, target_user_id=target_id, username=target_un)
        except Exception:
            ok = False

        # reschedule for person if registered id
        notif = context.application.bot_data.get("notif_service")
        if ok and target_id and notif:
            try:
                await notif.reschedule_for_person(target_id)
            except Exception as e:
                self.log.exception("reschedule after delete member failed: %s", e)

        await update.message.reply_text("участник удалён." if ok else "не удалось удалить участника.")
        await self._render_group_members(update, gid)
        await update.message.reply_text("выберите действие:", reply_markup=group_mgmt_kb())
        return ConversationHandler.END

    def conversation_handlers(self):
        return [
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex("^➕ создать группу$"), self.create_start)],
                states={STATE_WAIT_GROUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.create_wait_name)]},
                fallbacks=[MessageHandler(filters.Regex("^◀️ отмена$"), self.menu_entry)],
                name="conv_group_create",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex("^🔑 присоединиться к группе$"), self.join_start)],
                states={STATE_WAIT_JOIN_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.join_wait_code)]},
                fallbacks=[MessageHandler(filters.Regex("^◀️ отмена$"), self.menu_entry)],
                name="conv_group_join",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex("^🚪 покинуть группу$"), self.leave_start)],
                states={STATE_WAIT_LEAVE_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.leave_wait_code)]},
                fallbacks=[MessageHandler(filters.Regex("^◀️ отмена$"), self.menu_entry)],
                name="conv_group_leave",
                persistent=False,
            ),
            MessageHandler(filters.Regex("^📝 управление группами$"), self.manage_menu),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(r"^🛠 .+ \(.+\)$"), self.manage_entry)],
                states={},
                fallbacks=[],
                name="conv_group_manage_entry",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex("^✏️ переименовать группу$"), self.rename_start)],
                states={STATE_WAIT_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.rename_wait)]},
                fallbacks=[MessageHandler(filters.Regex("^◀️ отмена$"), self.manage_entry)],
                name="conv_group_rename",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex("^➕ добавить участника$"), self.add_member_start)],
                states={STATE_WAIT_ADD_MEMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_member_wait)]},
                fallbacks=[MessageHandler(filters.Regex("^◀️ отмена$"), self.manage_entry)],
                name="conv_group_add_member",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex("^🗑 удалить участника$"), self.del_member_start)],
                states={STATE_WAIT_DEL_MEMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.del_member_wait)]},
                fallbacks=[MessageHandler(filters.Regex("^◀️ отмена$"), self.manage_entry)],
                name="conv_group_del_member",
                persistent=False,
            ),
        ]
