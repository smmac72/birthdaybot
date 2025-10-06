from __future__ import annotations
import logging
import uuid
from typing import Dict, Tuple, Optional, Any, List

from telegram import Update
from telegram.ext import ContextTypes

from ..db.repo_users import UsersRepo
from ..db.repo_groups import GroupsRepo
from ..db.repo_friends import FriendsRepo
from ..keyboards import main_menu_kb

# helpers

def _log_id() -> str:
    return uuid.uuid4().hex[:8]

def _icon_registered(user_id: Optional[int]) -> str:
    return "✅" if user_id else "⚪️"

def _display_name(user_id: Optional[int], username: Optional[str]) -> str:
    if username:
        return f"@{username}"
    if user_id:
        return f"id:{user_id}"
    return "unknown"

def _fmt_bday(d: Optional[int], m: Optional[int], y: Optional[int]) -> str:
    if d and m:
        return f"{int(d):02d}-{int(m):02d}" + (f"-{int(y)}" if y else "")
    return "не указан"

def _days_until(today_ymd: Tuple[int,int,int], d: Optional[int], m: Optional[int]) -> int:
    # sort key by upcoming birthday, big sentinel if unknown
    if not d or not m:
        return 10**9
    import datetime as dt
    ty, tm, td = today_ymd
    try:
        target = dt.date(ty, m, d)
    except ValueError:
        return 10**9
    today = dt.date(ty, tm, td)
    if target < today:
        target = target.replace(year=ty + 1)
    return (target - today).days

def _when_str(days: int) -> str:
    if days == 0:
        return "сегодня"
    if days >= 10**8:
        return "дата не указана"
    return f"через {days} дн."

class BirthdaysHandler:
    def __init__(self, users: UsersRepo, friends: FriendsRepo, groups: GroupsRepo):
        self.users = users
        self.friends = friends
        self.groups = groups
        self.log = logging.getLogger("birthdays")

    async def menu_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        rid = _log_id()
        uid = update.effective_user.id
        self.log.info("[%s] birthdays entry: uid=%s", rid, uid)

        # today key
        import datetime as dt
        t = dt.date.today()
        tkey = (t.year, t.month, t.day)

        # merged contacts with sources and group names
        merged: Dict[Tuple[str,str], Dict[str, Any]] = {}

        # 1) friends
        try:
            fr = await self.friends.list_for_user(uid)
        except Exception as e:
            self.log.exception("[%s] list friends failed: %s", rid, e)
            fr = []

        for r in fr:
            r = dict(r)
            key: Tuple[str,str]
            if r.get("friend_user_id"):
                key = ("id", str(r["friend_user_id"]))
            else:
                key = ("u", (r.get("friend_username") or "").lower() or "unknown")

            merged[key] = {
                "user_id": r.get("friend_user_id"),
                "username": r.get("friend_username"),
                "birth_day": r.get("birth_day"),
                "birth_month": r.get("birth_month"),
                "birth_year": r.get("birth_year"),
                "sources": {"friend"},
                "groups": set(),  # will fill below
            }

        # 2) group members from all user's groups (attach group names)
        try:
            my_groups = await self.groups.list_user_groups(uid)
        except Exception:
            my_groups = []

        for g in my_groups:
            g = dict(g)
            gid, gname = g["group_id"], g["name"]
            try:
                members = await self.groups.list_members(gid)
            except Exception:
                members = []
            for m in members:
                m = dict(m)
                if m.get("user_id") == uid:
                    continue
                if m.get("user_id"):
                    key = ("id", str(m["user_id"]))
                else:
                    key = ("u", (m.get("username") or "").lower() or "unknown")

                if key not in merged:
                    merged[key] = {
                        "user_id": m.get("user_id"),
                        "username": m.get("username"),
                        "birth_day": m.get("birth_day"),
                        "birth_month": m.get("birth_month"),
                        "birth_year": m.get("birth_year"),
                        "sources": {"group"},
                        "groups": set([gname]),
                    }
                else:
                    merged[key]["sources"].add("group")
                    merged[key]["groups"].add(gname)
                    # prefer filled birthday
                    if not merged[key].get("birth_day") and m.get("birth_day"):
                        merged[key]["birth_day"] = m.get("birth_day")
                        merged[key]["birth_month"] = m.get("birth_month")
                        merged[key]["birth_year"] = m.get("birth_year")

        if not merged:
            await update.message.reply_text("пока нет контактов для показа.", reply_markup=main_menu_kb())
            return

        items: List[Dict[str, Any]] = list(merged.values())
        items.sort(key=lambda v: _days_until(tkey, v.get("birth_day"), v.get("birth_month")))

        # build unified lines
        lines = ["ближайшие дни рождения:\n"]
        for v in items:
            icon = _icon_registered(v.get("user_id"))
            name = _display_name(v.get("user_id"), v.get("username"))
            bd = _fmt_bday(v.get("birth_day"), v.get("birth_month"), v.get("birth_year"))
            dleft = _days_until(tkey, v.get("birth_day"), v.get("birth_month"))
            when = _when_str(dleft)

            badges = []
            if "friend" in v["sources"]:
                badges.append("ДРУГ")
            if "group" in v["sources"]:
                badges.append("В ГРУППЕ")
            badge_str = f" [{' & '.join(badges)}]" if badges else ""

            groups_note = ""
            if v["groups"]:
                gsample = sorted(v["groups"])
                if len(gsample) > 2:
                    groups_note = f" (в группах: {', '.join(gsample[:2])} …)"
                else:
                    groups_note = f" (в группах: {', '.join(gsample)})"

            lines.append(f"{icon} {name} — {bd} ({when}){badge_str}{groups_note}")

        await update.message.reply_text("\n".join(lines), reply_markup=main_menu_kb())
