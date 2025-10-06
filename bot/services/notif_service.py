from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import aiosqlite
import asyncio

from telegram.ext import Application, ContextTypes

from .. import config
from ..db.repo_users import UsersRepo
from ..db.repo_groups import GroupsRepo
from ..db.repo_friends import FriendsRepo

# small helpers

def _as_int(v, default: int = 0) -> int:
    try:
        if v is None:
            return default
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            v = v.strip()
            if v.upper() == "UTC":
                return 0
            return int(v)
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

    # ---------- storage for sent dedupe ----------

    async def _ensure_notif_schema(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications_sent(
                person_id   INTEGER NOT NULL,
                follower_id INTEGER NOT NULL,
                date_ymd    TEXT    NOT NULL,
                PRIMARY KEY(person_id, follower_id, date_ymd)
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_notif_date ON notifications_sent(date_ymd)")
        await db.commit()

    async def _already_sent(self, *, person_id: int, follower_id: int, date_ymd: str) -> bool:
        async with aiosqlite.connect(config.DB_PATH) as db:
            await self._ensure_notif_schema(db)
            cur = await db.execute(
                "SELECT 1 FROM notifications_sent WHERE person_id=? AND follower_id=? AND date_ymd=? LIMIT 1",
                (person_id, follower_id, date_ymd),
            )
            row = await cur.fetchone()
            return bool(row)

    async def _mark_sent(self, *, person_id: int, follower_id: int, date_ymd: str) -> None:
        async with aiosqlite.connect(config.DB_PATH) as db:
            await self._ensure_notif_schema(db)
            await db.execute(
                "INSERT OR IGNORE INTO notifications_sent(person_id, follower_id, date_ymd) VALUES(?,?,?)",
                (person_id, follower_id, date_ymd),
            )
            await db.commit()

    async def _cleanup_sent(self, *, keep_days: int = 400) -> int:
        cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(config.DB_PATH) as db:
            await self._ensure_notif_schema(db)
            cur = await db.execute("DELETE FROM notifications_sent WHERE date_ymd < ?", (cutoff,))
            await db.commit()
            try:
                return int(cur.rowcount or 0)
            except Exception:
                return 0

    # ---------- public api ----------

    async def schedule_all(self, horizon_days: int = 7) -> None:
        # pre-schedule all upcoming birthday notifications inside horizon
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

        # light cleanup in background
        try:
            removed = await self._cleanup_sent(keep_days=400)
            if removed:
                self.log.info("cleanup: removed %s old notification marks", removed)
        except Exception:
            pass

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

            # person local midnight for upcoming birthday date
            person_tz = _tz_from_offset(u.tz)
            next_date = _next_birthday_date(u.birth_day, u.birth_month, u.birth_year, today_utc)
            if not next_date:
                continue
            bday_local = dt.datetime.combine(next_date, dt.time(0, 0, tzinfo=person_tz))
            bday_date_ymd = next_date.strftime("%Y-%m-%d")

            # horizon cut
            if (bday_local.date() - today_utc).days > horizon_days:
                continue

            # followers union
            followers = await self._followers_union(u.user_id, (u.username or "").lower() if u.username else None)

            # per follower compute trigger
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

                # skip if already sent for this person/follower/date
                if await self._already_sent(person_id=u.user_id, follower_id=fid, date_ymd=bday_date_ymd):
                    continue

                # catch-up if trigger in past but within 12h window
                if trigger_utc <= now_utc and (now_utc - trigger_utc) <= dt.timedelta(hours=12):
                    await self._fire_direct(
                        follower_id=fid,
                        person_id=u.user_id,
                        person_username=u.username,
                        person_birth=(u.birth_day, u.birth_month, u.birth_year),
                        alert_hours=alert_h,
                        bday_date_ymd=bday_date_ymd,
                    )
                    cnt_catchup += 1
                    continue

                if trigger_utc <= now_utc:
                    # too old, skip silently
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
                        # pass local midnight to recover exact birthday date in tz-independent way
                        "bday_at_local": bday_local.isoformat(),
                        "bday_date_ymd": bday_date_ymd,
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

        # use tz string if valid, otherwise utc
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
        for fid in followers:
            if fid == person_id:
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
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=f"🧪 test alert: person id:{person_id} in {hours}h.",
                )
                sent += 1
            except Exception as e:
                self.log.exception("test send failed to %s: %s", fid, e)
        return sent

    # reschedule surface

    async def reschedule_for_person(self, person_id: int, username: Optional[str] = None) -> None:
        # cancel any jobs for this person and rebuild for current followers
        jq = getattr(self.app, "job_queue", None)
        if not jq:
            return
        # cancel old
        for j in self._iter_jobs():
            if j.name and j.name.startswith(f"bday:{person_id}:"):
                try:
                    j.schedule_removal()
                except Exception:
                    pass
        # then schedule anew
        await self.schedule_all(self._last_horizon)

    async def reschedule_for_follower(self, follower_id: int) -> None:
        # cancel all jobs targeted at follower and rebuild
        jq = getattr(self.app, "job_queue", None)
        if not jq:
            return
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
                return list(getattr(jq, "scheduler").queue)  # type: ignore[attr-defined]
            except Exception:
                return []

    async def _daily_refresh_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await self.schedule_all(self._last_horizon)
        except Exception as e:
            self.log.exception("daily refresh failed: %s", e)

    async def _fire_direct(
        self,
        *,
        follower_id: int,
        person_id: int,
        person_username: Optional[str],
        person_birth: Tuple[Optional[int], Optional[int], Optional[int]],
        alert_hours: int,
        bday_date_ymd: Optional[str] = None,
    ) -> None:
        # immediate send used for catch-up
        fprof = await self.users.get_user(follower_id)
        if not fprof:
            return
        chat_id = fprof.get("chat_id")
        if not chat_id:
            return

        pprof = await self.users.get_user(person_id)
        uname = (pprof.get("username") if pprof else person_username) or f"id:{person_id}"

        d, m, y = person_birth
        age_part = ""
        try:
            if y and d and m:
                today = dt.date.today()
                nxt = _next_birthday_date(int(d), int(m), int(y), today)
                if nxt:
                    age_part = f" (turns {nxt.year - int(y)})"
        except Exception:
            age_part = ""

        if _as_int(alert_hours, 0) == 0:
            msg = f"🎂 today is @{uname}'s birthday{age_part}! 🎉"
        else:
            msg = f"🎂 @{uname}{age_part} has a birthday in {int(alert_hours)}h! 🎉"

        try:
            await self.app.bot.send_message(chat_id=chat_id, text=msg)
            # mark sent for dedupe
            date_ymd = bday_date_ymd
            if not date_ymd and d and m:
                # fallback compute date for marking
                today = dt.date.today()
                nxt = _next_birthday_date(int(d), int(m), y, today)
                if nxt:
                    date_ymd = nxt.strftime("%Y-%m-%d")
            if date_ymd:
                await self._mark_sent(person_id=person_id, follower_id=follower_id, date_ymd=date_ymd)
        except Exception as e:
            self.log.exception("send failed: %s", e)

    async def _fire_one(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        data = context.job.data or {}
        person_id = data.get("person_id")
        follower_id = data.get("follower_id")
        alert_h = _as_int(data.get("alert_hours"), 0)

        fprof = await self.users.get_user(follower_id)
        if not fprof:
            return
        chat_id = fprof.get("chat_id")
        if not chat_id:
            return

        pprof = await self.users.get_user(person_id)
        uname = (pprof.get("username") if pprof else data.get("person_username")) or f"id:{person_id}"

        d, m, y = (data.get("person_birth") or (None, None, None))
        age_part = ""
        try:
            if y and d and m:
                today = dt.date.today()
                nxt = _next_birthday_date(int(d), int(m), int(y), today)
                if nxt:
                    age_part = f" (turns {nxt.year - int(y)})"
        except Exception:
            age_part = ""

        if alert_h == 0:
            msg = f"🎂 today is @{uname}'s birthday{age_part}! 🎉"
        else:
            msg = f"🎂 @{uname}{age_part} has a birthday in {alert_h}h! 🎉"

        try:
            await self.app.bot.send_message(chat_id=chat_id, text=msg)
            # mark sent
            date_ymd = data.get("bday_date_ymd")
            if not date_ymd:
                try:
                    bdl = data.get("bday_at_local")
                    if isinstance(bdl, str) and len(bdl) >= 10:
                        date_ymd = bdl[:10]
                except Exception:
                    pass
            if not date_ymd and d and m:
                today = dt.date.today()
                nxt = _next_birthday_date(int(d), int(m), y, today)
                if nxt:
                    date_ymd = nxt.strftime("%Y-%m-%d")
            if date_ymd:
                await self._mark_sent(person_id=person_id, follower_id=follower_id, date_ymd=date_ymd)
        except Exception as e:
            self.log.exception("send failed: %s", e)

    async def _followers_co_members(self, user_id: int) -> List[int]:
        # co-members across all groups where user participates
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
            owners = await self.friends.list_owners_for_person(
                person_user_id=person_id,
                username_lower=username_lower,
            )
            return [o for o in owners if o and o != person_id]
        except Exception as e:
            self.log.exception("friends followers query failed: %s", e)
            return []

    async def _followers_union(self, person_id: int, username_lower: Optional[str]) -> List[int]:
        a = await self._followers_co_members(person_id)
        b = await self._followers_via_friends(person_id, username_lower)
        return list({x for x in (a + b) if x})

    # tiny debug util
    async def debug_followers(self, person_id: int) -> str:
        folks = await self._followers_union(person_id, None)
        return f"followers for {person_id}: {len(folks)} -> {sorted(folks)}"

    async def schedule_daily_cleanup(self, at_hour: int = 4) -> None:
        # run daily db cleanup for notifications_sent table
        jq = getattr(self.app, "job_queue", None)
        if not jq:
            self.log.info("job queue missing, skip daily cleanup")
            return

        for old in jq.get_jobs_by_name("daily_notif_cleanup"):
            try:
                old.schedule_removal()
            except Exception:
                pass

        # same tz logic as refresh
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(getattr(config, "DEFAULT_TZ", "UTC"))
        except Exception:
            tz = dt.timezone.utc

        jq.run_daily(self._daily_cleanup_job, time=dt.time(hour=at_hour, tzinfo=tz), name="daily_notif_cleanup")
        self.log.info("daily cleanup scheduled at %02d:00", at_hour)

    async def _daily_cleanup_job(self, _context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            removed = await self._cleanup_sent(keep_days=400)
            if removed:
                self.log.info("cleanup: removed %s old notification marks", removed)
        except Exception as e:
            self.log.exception("daily cleanup failed: %s", e)
