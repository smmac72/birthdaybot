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
    # fixed offset only, fallback utc
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

            # person local midnight
            person_tz = _tz_from_offset(u.tz)
            next_date = _next_birthday_date(u.birth_day, u.birth_month, u.birth_year, today_utc)
            if not next_date:
                continue
            bday_local = dt.datetime.combine(next_date, dt.time(0, 0, tzinfo=person_tz))

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

                # catch-up if already passed within 12h window
                if trigger_utc <= now_utc and (now_utc - trigger_utc) <= dt.timedelta(hours=12):
                    await self._fire_direct(
                        follower_id=fid,
                        person_id=u.user_id,
                        person_username=u.username,
                        person_birth=(u.birth_day, u.birth_month, u.birth_year),
                        alert_hours=alert_h,
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
                        "bday_at_local": bday_local.isoformat(),
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
                    text=f"🧪 тестовое уведомление: у участника id:{person_id} 'день рождения' через {hours} ч.",
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
        uname_l = (username or "").lower() if username else None
        # cancel old
        for j in self._iter_jobs():
            if j.name and j.name.startswith(f"bday:{person_id}:"):
                try:
                    j.schedule_removal()
                except Exception:
                    pass
        # then schedule anew (inside horizon and catch-up included)
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
            # fallback for older ptb if api differs
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
                    age_part = f" (исполнится {nxt.year - int(y)})"
        except Exception:
            age_part = ""

        if _as_int(alert_hours, 0) == 0:
            msg = f"🎂 сегодня день рождения у @{uname}{age_part}! 🎉"
        else:
            msg = f"🎂 у @{uname}{age_part} день рождения через {int(alert_hours)} ч.! 🎉"

        try:
            await self.app.bot.send_message(chat_id=chat_id, text=msg)
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
                    age_part = f" (исполнится {nxt.year - int(y)})"
        except Exception:
            age_part = ""

        if alert_h == 0:
            msg = f"🎂 сегодня день рождения у @{uname}{age_part}! 🎉"
        else:
            msg = f"🎂 у @{uname}{age_part} день рождения через {alert_h} ч.! 🎉"

        try:
            await self.app.bot.send_message(chat_id=chat_id, text=msg)
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
        # delegate to friends repo to avoid direct db usage
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
