from __future__ import annotations

import logging
import re
import uuid
import datetime as dt
from typing import Optional, List, Dict, Any

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from ..db.repo_groups import GroupsRepo
from ..db.repo_users import UsersRepo
from ..i18n import t, btn_regex
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

def _cancel_kb(update=None, context=None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[t("btn_cancel", update=update, context=context)]], resize_keyboard=True, one_time_keyboard=True)

def _icon_registered(user_id: Optional[int]) -> str:
    return "✅" if user_id else "⚪️"

def _fmt_bday(d, m, y, *, update=None, context=None) -> str:
    if d and m:
        return f"{int(d):02d}-{int(m):02d}" + (f"-{int(y)}" if y else "")
    return t("when_unknown", update=update, context=context)

def _days_until_key(d: Optional[int], m: Optional[int]) -> int:
    if not d or not m:
        return 10**9
    today = dt.date.today()
    try:
        target = dt.date(today.year, m, d)
    except ValueError:
        return 10**9
    if target < today:
        target = target.replace(year=today.year + 1)
    return (target - today).days

def _when_str(days: int, *, update=None, context=None) -> str:
    if days == 0:
        return t("when_today", update=update, context=context)
    if days >= 10**8:
        return t("when_unknown", update=update, context=context)
    return t("when_in_days", update=update, context=context, n=days)

def _member_line(m: Dict[str, Any], *, update=None, context=None) -> str:
    icon = _icon_registered(m.get("user_id"))
    name = f"@{m['username']}" if m.get("username") else (f"id:{m['user_id']}" if m.get("user_id") else "unknown")
    bd = _fmt_bday(m.get("birth_day"), m.get("birth_month"), m.get("birth_year"), update=update, context=context)
    dleft = _days_until_key(m.get("birth_day"), m.get("birth_month"))
    when = _when_str(dleft, update=update, context=context)
    return f"• {icon} {name} — {bd} ({when})"

class GroupsHandler:
    def __init__(self, groups: GroupsRepo, users: UsersRepo):
        self.groups = groups
        self.users = users
        self.log = logging.getLogger("groups")

    # main groups list
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
            await update.message.reply_text(t("not_found", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
            return

        if not rows:
            await update.message.reply_text(t("groups_none", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
            return

        lines = [t("groups_list_header", update=update, context=context)]
        for g in rows:
            g = dict(g)
            mark = t("groups_creator_mark", update=update, context=context) if g.get("creator_user_id") == uid else ""
            lines.append(f"📌 {g['name']} ({t('groups_code', update=update, context=context, code=g['code'])}) — {int(g.get('member_count', 0))} {t('groups_members_word', update=update, context=context)}{mark}")

        await update.message.reply_text("\n\n".join(["\n".join(lines), t("groups_manage_prompt", update=update, context=context)]), reply_markup=groups_menu_kb(update=update, context=context))

    # managed groups list
    async def manage_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        try:
            rows = await self.groups.list_user_groups(uid)
        except Exception:
            rows = []
        managed = [dict(r) for r in rows if int(r.get("creator_user_id", 0)) == uid]

        if not managed:
            await update.message.reply_text(t("groups_manage_need", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
            return

        # show as "🛠 {name} ({code})"
        kb = ReplyKeyboardMarkup(
            [[f"🛠 {g['name']} ({g['code']})"] for g in managed] + [[t("btn_exit", update=update, context=context)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text(t("groups_manage_pick", update=update, context=context), reply_markup=kb)

    async def _render_group_members(self, update: Update, gid: str, context: ContextTypes.DEFAULT_TYPE) -> List[Dict[str, Any]]:
        members = await self.groups.list_members(gid)
        members = [dict(m) for m in members]
        members.sort(key=lambda m: _days_until_key(m.get("birth_day"), m.get("birth_month")))
        lines = [t("groups_members_header", update=update, context=context, n=len(members))]
        for m in members:
            lines.append(_member_line(m, update=update, context=context))
        await update.message.reply_text("\n".join(lines))
        return members

    # create
    async def create_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(t("groups_create_prompt", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
        return STATE_WAIT_GROUP_NAME

    async def create_wait_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "").strip()
        if text == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(t("canceled", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
            return ConversationHandler.END
        name = text
        gid, code = await self.groups.create_group(name, update.effective_user.id)
        await update.message.reply_text(t("groups_created", update=update, context=context, name=name, code=code), reply_markup=groups_menu_kb(update=update, context=context))

        # reschedule for creator as follower+person (safe: just rebuild all for person)
        notif = context.application.bot_data.get("notif_service")
        if notif:
            try:
                await notif.reschedule_for_person(update.effective_user.id, update.effective_user.username)
            except Exception as e:
                self.log.exception("reschedule after create group failed: %s", e)

        return ConversationHandler.END

    # join
    async def join_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(t("groups_join_prompt", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
        return STATE_WAIT_JOIN_CODE

    async def join_wait_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        code = (update.message.text or "").strip()
        if code == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(t("canceled", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
            return ConversationHandler.END
        ok, name = await self.groups.join_by_code(code, update.effective_user.id)
        if ok:
            await update.message.reply_text(t("groups_join_ok", update=update, context=context, name=name), reply_markup=groups_menu_kb(update=update, context=context))
            notif = context.application.bot_data.get("notif_service")
            if notif:
                try:
                    await notif.reschedule_for_person(update.effective_user.id, update.effective_user.username)
                except Exception as e:
                    self.log.exception("reschedule after join failed: %s", e)
        else:
            await update.message.reply_text(t("groups_join_fail", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
        return ConversationHandler.END

    # leave
    async def leave_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(t("groups_leave_prompt", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
        return STATE_WAIT_LEAVE_CODE

    async def leave_wait_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        code = (update.message.text or "").strip()
        if code == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(t("canceled", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
            return ConversationHandler.END
        ok, name = await self.groups.leave_by_code(code, update.effective_user.id)
        if ok:
            await update.message.reply_text(t("groups_leave_ok", update=update, context=context, name=name), reply_markup=groups_menu_kb(update=update, context=context))
            notif = context.application.bot_data.get("notif_service")
            if notif:
                try:
                    await notif.reschedule_for_person(update.effective_user.id, update.effective_user.username)
                except Exception as e:
                    self.log.exception("reschedule after leave failed: %s", e)
        else:
            await update.message.reply_text(t("groups_leave_fail", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
        return ConversationHandler.END

    # manage entry
    async def manage_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "")
        m = re.match(r"^🛠\s+(.+)\s+\(([\w-]+)\)$", text)
        if not m:
            await update.message.reply_text(t("groups_pick_from_menu", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
            return ConversationHandler.END
        code = m.group(2)
        g = await self.groups.get_by_code(code)
        if not g:
            await update.message.reply_text(t("groups_not_found", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
            return ConversationHandler.END

        gid = g["group_id"]
        context.user_data["mgmt_gid"] = gid

        await update.message.reply_text(f"{t('groups_one_title', update=update, context=context)} {g['name']}")
        await self._render_group_members(update, gid, context)
        await update.message.reply_text(t("groups_manage_prompt", update=update, context=context), reply_markup=group_mgmt_kb(update=update, context=context))
        return ConversationHandler.END

    # rename
    async def rename_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        gid = context.user_data.get("mgmt_gid")
        if not gid:
            await update.message.reply_text(t("groups_manage_need", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
            return ConversationHandler.END
        await update.message.reply_text(t("groups_rename_prompt", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
        return STATE_WAIT_RENAME

    async def rename_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        gid = context.user_data.get("mgmt_gid")
        if not gid:
            await update.message.reply_text(t("groups_manage_need", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
            return ConversationHandler.END
        text = (update.message.text or "").strip()
        if text == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(t("canceled", update=update, context=context), reply_markup=group_mgmt_kb(update=update, context=context))
            return ConversationHandler.END
        await self.groups.rename_group(gid, text)
        await update.message.reply_text(t("groups_rename_ok", update=update, context=context))
        await self._render_group_members(update, gid, context)
        await update.message.reply_text(t("groups_manage_prompt", update=update, context=context), reply_markup=group_mgmt_kb(update=update, context=context))
        return ConversationHandler.END

    # add member
    async def add_member_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        gid = context.user_data.get("mgmt_gid")
        if not gid:
            await update.message.reply_text(t("groups_manage_need", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
            return ConversationHandler.END
        await update.message.reply_text(
            t("groups_add_member_prompt", update=update, context=context),
            reply_markup=_cancel_kb(update=update, context=context),
        )
        return STATE_WAIT_ADD_MEMBER

    async def add_member_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        gid = context.user_data.get("mgmt_gid")
        if not gid:
            await update.message.reply_text(t("groups_manage_need", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
            return ConversationHandler.END

        text = (update.message.text or "").strip()
        if text == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(t("canceled", update=update, context=context), reply_markup=group_mgmt_kb(update=update, context=context))
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
            # validate calendar, drop non-leap 29 feb year
            try:
                if y:
                    dt.date(y, mo, d)
            except ValueError:
                if not (mo == 2 and d == 29):
                    await update.message.reply_text(t("groups_add_member_need_date", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
                    return STATE_WAIT_ADD_MEMBER
                y = None
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
            notif = context.application.bot_data.get("notif_service")
            if notif:
                try:
                    await notif.reschedule_for_person(prof.get("user_id"), prof.get("username"))
                except Exception as e:
                    self.log.exception("reschedule add member failed: %s", e)
            await update.message.reply_text(t("groups_add_member_ok", update=update, context=context))
        else:
            if not bd:
                await update.message.reply_text(t("groups_add_member_need_date", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
                return STATE_WAIT_ADD_MEMBER
            d, mo, y = bd
            await self.groups.add_member(gid, user_id, username, d, mo, y)
            await update.message.reply_text(t("groups_add_member_ok", update=update, context=context))

        await self._render_group_members(update, gid, context)
        await update.message.reply_text(t("groups_manage_prompt", update=update, context=context), reply_markup=group_mgmt_kb(update=update, context=context))
        return ConversationHandler.END

    # delete member
    async def del_member_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        gid = context.user_data.get("mgmt_gid")
        if not gid:
            await update.message.reply_text(t("groups_manage_need", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
            return ConversationHandler.END
        await update.message.reply_text(t("groups_del_member_prompt", update=update, context=context), reply_markup=_cancel_kb(update=update, context=context))
        return STATE_WAIT_DEL_MEMBER

    async def del_member_wait(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        gid = context.user_data.get("mgmt_gid")
        if not gid:
            await update.message.reply_text(t("groups_manage_need", update=update, context=context), reply_markup=groups_menu_kb(update=update, context=context))
            return ConversationHandler.END

        text = (update.message.text or "").strip()
        if text == t("btn_cancel", update=update, context=context):
            await update.message.reply_text(t("canceled", update=update, context=context), reply_markup=group_mgmt_kb(update=update, context=context))
            return ConversationHandler.END

        if text.isdigit():
            target_id = int(text)
            target_un = None
        elif text.startswith("@"):
            target_id = None
            target_un = text[1:]
        else:
            await update.message.reply_text(t("groups_del_member_prompt", update=update, context=context))
            return STATE_WAIT_DEL_MEMBER

        # do not allow kicking self
        if target_id and target_id == update.effective_user.id:
            await update.message.reply_text(t("groups_del_member_self", update=update, context=context), reply_markup=group_mgmt_kb(update=update, context=context))
            return ConversationHandler.END

        ok = False
        try:
            ok = await self.groups.remove_member(gid, target_user_id=target_id, username=target_un)
        except Exception:
            ok = False

        # reschedule for person if id known (priority item 1)
        notif = context.application.bot_data.get("notif_service")
        if ok and target_id:
            notif = context.application.bot_data.get("notif_service")
            if notif:
                try:
                    await notif.reschedule_for_person(target_id)
                except Exception as e:
                    self.log.exception("reschedule after delete member failed: %s", e)

        await update.message.reply_text(t("groups_del_member_ok", update=update, context=context) if ok else t("groups_del_member_fail", update=update, context=context))
        await self._render_group_members(update, gid, context)
        await update.message.reply_text(t("groups_manage_prompt", update=update, context=context), reply_markup=group_mgmt_kb(update=update, context=context))
        return ConversationHandler.END

    def conversation_handlers(self):
        return [
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(btn_regex("btn_group_create")), self.create_start)],
                states={STATE_WAIT_GROUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.create_wait_name)]},
                fallbacks=[MessageHandler(filters.Regex(btn_regex("btn_cancel")), self.menu_entry)],
                name="conv_group_create",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(btn_regex("btn_group_join")), self.join_start)],
                states={STATE_WAIT_JOIN_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.join_wait_code)]},
                fallbacks=[MessageHandler(filters.Regex(btn_regex("btn_cancel")), self.menu_entry)],
                name="conv_group_join",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(btn_regex("btn_group_leave")), self.leave_start)],
                states={STATE_WAIT_LEAVE_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.leave_wait_code)]},
                fallbacks=[MessageHandler(filters.Regex(btn_regex("btn_cancel")), self.menu_entry)],
                name="conv_group_leave",
                persistent=False,
            ),
            MessageHandler(filters.Regex(btn_regex("btn_group_manage")) | filters.Regex(r"^\s*🛠"), self.manage_menu),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(r"^🛠 .+ \(.+\)$"), self.manage_entry)],
                states={},
                fallbacks=[],
                name="conv_group_manage_entry",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(btn_regex("btn_group_rename")), self.rename_start)],
                states={STATE_WAIT_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.rename_wait)]},
                fallbacks=[MessageHandler(filters.Regex(btn_regex("btn_cancel")), self.manage_entry)],
                name="conv_group_rename",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(btn_regex("btn_group_add_member")), self.add_member_start)],
                states={STATE_WAIT_ADD_MEMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_member_wait)]},
                fallbacks=[MessageHandler(filters.Regex(btn_regex("btn_cancel")), self.manage_entry)],
                name="conv_group_add_member",
                persistent=False,
            ),
            ConversationHandler(
                entry_points=[MessageHandler(filters.Regex(btn_regex("btn_group_del_member")), self.del_member_start)],
                states={STATE_WAIT_DEL_MEMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.del_member_wait)]},
                fallbacks=[MessageHandler(filters.Regex(btn_regex("btn_cancel")), self.manage_entry)],
                name="conv_group_del_member",
                persistent=False,
            ),
        ]
