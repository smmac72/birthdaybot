from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple, Dict

from telegram.ext import Application, ContextTypes

from .. import config
from ..db.repo_users import UsersRepo
from ..db.repo_groups import GroupsRepo
from ..db.repo_friends import FriendsRepo
from ..i18n import t

def _as_int(v, default: int = 0) -> int:
    try:
        if v is None:
            return default
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            s = v.strip()
            if s.upper() == "UTC":
                return 0
            return int(s)
        return int(v)
    except Exception:
        return default

def _tz_from_offset(hours: Optional[int]) -> dt.tzinfo:
    h = _as_int(hours, 0)
    return dt.timezone(dt.timedelta(hours=h))

def _next_birthday_date(bd: int, bm: int, by: Optional[int], today: dt.date) -> Optional[dt.date]:
    try:
        cand = dt.date(today.year, bm, bd)
        if cand < today:
            cand = dt.date(today.year + 1, bm, bd)
        return cand
    except Exception:
        return None

def _job_name(person_id: int, follower_id: int, when_utc: dt.datetime) -> str:
    return f"bday:{person_id}:{follower_id}:{when_utc.strftime('%Y%m%d%H%M')}"

@dataclass
class _UserRow:
    user_id: int
    username: Optional[str]
    birth_day: Optional[int]
    birth_month: Optional[int]
    birth_year: Optional[int]
    tz: int
    chat_id: Optional[int]

class NotifService:
    def __init__(self, app: Application, users: UsersRepo, groups: GroupsRepo, friends: FriendsRepo) -> None:
        self.app = app
        self.users = users
        self.groups = groups
        self.friends = friends
        self.log = logging.getLogger("notif")
        self._last_horizon: int = getattr(config, "SCHEDULE_HORIZON_DAYS", 7)

    # ---------- helpers for dedupe ----------

    async def _already_sent(self, *, person_id: int, follower_id: int, date_ymd: str) -> bool:
        # schema: notifications_sent(person_id, follower_id, date_ymd) unique
        import aiosqlite
        async with aiosqlite.connect(self.users.db_path) as db:  # reuse same file
            await db.execute("""
                CREATE TABLE IF NOT EXISTS notifications_sent(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id INTEGER NOT NULL,
                    follower_id INTEGER NOT NULL,
                    date_ymd TEXT NOT NULL,
                    UNIQUE(person_id, follower_id, date_ymd)
                )
            """)
            cur = await db.execute(
                "SELECT 1 FROM notifications_sent WHERE person_id=? AND follower_id=? AND date_ymd=?",
                (person_id, follower_id, date_ymd),
            )
            row = await cur.fetchone()
            return bool(row)

    async def _mark_sent(self, *, person_id: int, follower_id: int, date_ymd: str) -> None:
        import aiosqlite, sqlite3
        async with aiosqlite.connect(self.users.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS notifications_sent(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id INTEGER NOT NULL,
                    follower_id INTEGER NOT NULL,
                    date_ymd TEXT NOT NULL,
                    UNIQUE(person_id, follower_id, date_ymd)
                )
            """)
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO notifications_sent(person_id, follower_id, date_ymd) VALUES(?,?,?)",
                    (person_id, follower_id, date_ymd),
                )
                await db.commit()
            except sqlite3.IntegrityError:
                pass

    # ---------- public api ----------

    async def schedule_all(self, horizon_days: int = 7) -> None:
        self._last_horizon = horizon_days
        jq = getattr(self.app, "job_queue", None)
        if not jq:
            self.log.info("job queue missing, skip schedule_all")
            return

        try:
            rows = await self.users.list_all_users_with_bday()
        except Exception as e:
            self.log.exception("users fetch failed: %s", e)
            return

        today_utc = dt.datetime.now(dt.timezone.utc).date()
        now_utc = dt.datetime.now(dt.timezone.utc)

        cnt_jobs = 0
        cnt_catchup = 0

        for r in rows:
            d = dict(r)
            u = _UserRow(
                user_id=int(d["user_id"]),
                username=d.get("username"),
                birth_day=int(d["birth_day"]) if d.get("birth_day") else None,
                birth_month=int(d["birth_month"]) if d.get("birth_month") else None,
                birth_year=int(d["birth_year"]) if d.get("birth_year") else None,
                tz=_as_int(d.get("tz"), 0),
                chat_id=d.get("chat_id"),
            )
            if not u.birth_day or not u.birth_month:
                continue

            person_tz = _tz_from_offset(u.tz)
            next_date = _next_birthday_date(u.birth_day, u.birth_month, u.birth_year, today_utc)
            if not next_date:
                continue
            # we'll dedupe per calendar birthday (person's local date)
            date_ymd = next_date.strftime("%Y-%m-%d")
            bday_local = dt.datetime.combine(next_date, dt.time(0, 0, tzinfo=person_tz))

            # horizon cut
            if (bday_local.date() - today_utc).days > horizon_days:
                continue

            followers = await self._followers_union(u.user_id, (u.username or "").lower() if u.username else None)

            for fid in followers:
                if fid == u.user_id:
                    continue
                fprof = await self.users.get_user(fid)
                if not fprof:
                    continue
                f_tz = _tz_from_offset(_as_int(fprof.get("tz"), 0))
                alert_h = _as_int(fprof.get("alert_hours"), 0)
                bday_in_f_tz = bday_local.astimezone(f_tz)
                trigger_local = bday_in_f_tz - dt.timedelta(hours=alert_h)
                trigger_utc = trigger_local.astimezone(dt.timezone.utc)

                # catch-up with dedupe
                if trigger_utc <= now_utc and (now_utc - trigger_utc) <= dt.timedelta(hours=12):
                    if not await self._already_sent(person_id=u.user_id, follower_id=fid, date_ymd=date_ymd):
                        await self._fire_direct(follower_id=fid, person_id=u.user_id, person_username=u.username, person_birth=(u.birth_day, u.birth_month, u.birth_year), alert_hours=alert_h, date_ymd=date_ymd)
                        cnt_catchup += 1
                    continue

                if trigger_utc <= now_utc:
                    continue

                name = _job_name(u.user_id, fid, trigger_utc)
                for old in jq.get_jobs_by_name(name):
                    try:
                        old.schedule_removal()
                    except Exception:
                        pass

                jq.run_once(
                    callback=self._fire_one,
                    when=trigger_utc,
                    data={
                        "person_id": u.user_id,
                        "person_username": u.username,
                        "person_birth": (u.birth_day, u.birth_month, u.birth_year),
                        "follower_id": fid,
                        "alert_hours": alert_h,
                        "bday_at_local": bday_local.isoformat(),
                        "date_ymd": date_ymd,
                    },
                    name=name,
                )
                cnt_jobs += 1

        self.log.info("scheduled %s jobs, catch-up sent %s", cnt_jobs, cnt_catchup)

    async def schedule_daily_refresh(self, at_hour: int = 3) -> None:
        jq = getattr(self.app, "job_queue", None)
        if not jq:
            self.log.info("job queue missing, skip daily refresh")
            return

        for old in jq.get_jobs_by_name("daily_bday_refresh"):
            try:
                old.schedule_removal()
            except Exception:
                pass

        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(getattr(config, "DEFAULT_TZ", "UTC"))
        except Exception:
            tz = dt.timezone.utc

        jq.run_daily(self._daily_refresh_job, time=dt.time(hour=at_hour, tzinfo=tz), name="daily_bday_refresh")
        self.log.info("daily refresh scheduled at %02d:00", at_hour)

    async def test_broadcast(self, person_id: int, hours: int) -> int:
        sent = 0
        followers = await self._followers_union(person_id, None)
        today = dt.date.today()
        date_ymd = today.strftime("%Y-%m-%d")
        for fid in followers:
            if fid == person_id:
                continue
            if await self._already_sent(person_id=person_id, follower_id=fid, date_ymd=date_ymd):
                continue
            prof = await self.users.get_user(fid)
            if not prof:
                continue
            if _as_int(prof.get("alert_hours"), 0) != _as_int(hours, 0):
                continue
            chat_id = prof.get("chat_id")
            if not chat_id:
                continue
            try:
                # keep it english/russian via t(), use follower context (we don't have update here, so context=None is fine)
                msg = f"🧪 {t('test_alert', context=None, update=None, hours=hours)}"
                await self.app.bot.send_message(chat_id=chat_id, text=msg)
                await self._mark_sent(person_id=person_id, follower_id=fid, date_ymd=date_ymd)
                sent += 1
            except Exception as e:
                self.log.exception("test send failed to %s: %s", fid, e)
        return sent

    async def reschedule_for_person(self, person_id: int, username: Optional[str] = None) -> None:
        jq = getattr(self.app, "job_queue", None)
        if not jq:
            return
        # cancel old jobs for this person
        for j in self._iter_jobs():
            if j.name and j.name.startswith(f"bday:{person_id}:"):
                try:
                    j.schedule_removal()
                except Exception:
                    pass
        # re-schedule window
        await self.schedule_all(self._last_horizon)

    async def reschedule_for_follower(self, follower_id: int) -> None:
        jq = getattr(self.app, "job_queue", None)
        if not jq:
            return
        # cancel jobs targeted at follower
        for j in self._iter_jobs():
            if not j.name:
                continue
            parts = j.name.split(":")
            if len(parts) >= 3 and parts[0] == "bday" and parts[2] == str(follower_id):
                try:
                    j.schedule_removal()
                except Exception:
                    pass
        await self.schedule_all(self._last_horizon)

    # ---------- internals ----------

    def _iter_jobs(self):
        jq = getattr(self.app, "job_queue", None)
        if not jq:
            return []
        try:
            return list(jq.jobs())
        except Exception:
            try:
                return list(getattr(jq, "scheduler").queue)  # pragma: no cover
            except Exception:
                return []

    async def _daily_refresh_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await self.schedule_all(self._last_horizon)
        except Exception as e:
            self.log.exception("daily refresh failed: %s", e)

    async def _compose_bday_message(self, *, follower_id: int, person_id: int, person_username: Optional[str], person_birth: Tuple[Optional[int], Optional[int], Optional[int]], alert_hours: int) -> str:
        # localize by follower's language; we don't have update, but t() can still format english/russian if we pass nothing (fallback from your i18n impl). if your i18n needs user context to choose language, you can stash language in users table later.
        uname = (person_username or f"id:{person_id}")
        d, m, y = person_birth
        age_part = ""
        try:
            if y and d and m:
                today = dt.date.today()
                nxt = _next_birthday_date(int(d), int(m), int(y), today)
                if nxt:
                    age_part = f" ({t('turns_age', update=None, context=None, age=(nxt.year - int(y)))})"
        except Exception:
            age_part = ""
        if _as_int(alert_hours, 0) == 0:
            # “today” variant
            return t("notif_today", update=None, context=None, username=uname, age_part=age_part)
        else:
            return t("notif_in_hours", update=None, context=None, username=uname, age_part=age_part, hours=int(alert_hours))

    async def _fire_direct(self, *, follower_id: int, person_id: int, person_username: Optional[str], person_birth: Tuple[Optional[int], Optional[int], Optional[int]], alert_hours: int, date_ymd: str) -> None:
        fprof = await self.users.get_user(follower_id)
        if not fprof:
            return
        chat_id = fprof.get("chat_id")
        if not chat_id:
            return

        msg = await self._compose_bday_message(follower_id=follower_id, person_id=person_id, person_username=person_username, person_birth=person_birth, alert_hours=alert_hours)
        try:
            await self.app.bot.send_message(chat_id=chat_id, text=msg)
            await self._mark_sent(person_id=person_id, follower_id=follower_id, date_ymd=date_ymd)
        except Exception as e:
            self.log.exception("send failed: %s", e)

    async def _fire_one(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        data = context.job.data or {}
        person_id = data.get("person_id")
        follower_id = data.get("follower_id")
        alert_h = _as_int(data.get("alert_hours"), 0)
        date_ymd = data.get("date_ymd") or ""

        # dedupe at send-time too
        if not person_id or not follower_id or not date_ymd:
            return
        if await self._already_sent(person_id=person_id, follower_id=follower_id, date_ymd=date_ymd):
            return

        p_username = data.get("person_username")
        d, m, y = (data.get("person_birth") or (None, None, None))

        # send
        await self._fire_direct(follower_id=follower_id, person_id=person_id, person_username=p_username, person_birth=(d, m, y), alert_hours=alert_h, date_ymd=date_ymd)

    async def _followers_co_members(self, user_id: int) -> List[int]:
        ids: List[int] = []
        try:
            rows = await self.groups.list_user_groups(user_id)
            for g in rows:
                members = await self.groups.list_members(g["group_id"])
                for m in members:
                    mid = m.get("user_id")
                    if isinstance(mid, int) and mid and mid not in ids:
                        ids.append(int(mid))
        except Exception:
            pass
        return [i for i in ids if i and i != user_id]

    async def _followers_via_friends(self, person_id: int, username_lower: Optional[str]) -> List[int]:
        try:
            owners = await self.friends.list_owners_for_person(person_user_id=person_id, username_lower=username_lower)
            return [o for o in owners if o and o != person_id]
        except Exception as e:
            self.log.exception("friends followers query failed: %s", e)
            return []

    async def _followers_union(self, person_id: int, username_lower: Optional[str]) -> List[int]:
        a = await self._followers_co_members(person_id)
        b = await self._followers_via_friends(person_id, username_lower)
        return list({x for x in (a + b) if x})
