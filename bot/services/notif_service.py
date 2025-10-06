from __future__ import annotations

# notif service: schedules and fires birthday alerts
# comments are chill and lowercase

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

# tiny helpers

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

def _safe_date(year: int, month: int, day: int) -> Optional[dt.date]:
    try:
        return dt.date(year, month, day)
    except ValueError:
        # handle feb 29 gracefully -> feb 28
        if month == 2 and day == 29:
            return dt.date(year, 2, 28)
        return None

def _next_birthday_date(bd: int, bm: int, by: Optional[int], today: dt.date) -> Optional[dt.date]:
    # next occurrence in calendar (respecting feb 29)
    cand = _safe_date(today.year, bm, bd)
    if not cand:
        return None
    if cand < today:
        cand = _safe_date(today.year + 1, bm, bd)
    return cand

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

                alert_days = fprof.get("alert_days")
                alert_time = (fprof.get("alert_time") or "00:00") if alert_days is not None else None
                alert_hours = fprof.get("alert_hours") if alert_days is None else None

                bday_in_f_tz = bday_local.astimezone(f_tz)

                if alert_days is not None:
                    try:
                        hh, mm = map(int, (alert_time or "00:00").split(":"))
                    except Exception:
                        hh, mm = 0, 0
                    trigger_local_date = bday_in_f_tz.date() - dt.timedelta(days=int(alert_days))
                    trigger_local = dt.datetime(
                        trigger_local_date.year, trigger_local_date.month, trigger_local_date.day, hh, mm, tzinfo=f_tz
                    )
                    trigger_utc = trigger_local.astimezone(dt.timezone.utc)
                    meta = {"model": "new", "alert_days": int(alert_days), "alert_time": f"{hh:02d}:{mm:02d}"}
                else:
                    # legacy: hours before local midnight of person (already in old code)
                    alert_h = _as_int(alert_hours, 0)
                    trigger_local = bday_in_f_tz - dt.timedelta(hours=alert_h)
                    trigger_utc = trigger_local.astimezone(dt.timezone.utc)
                    meta = {"model": "legacy", "alert_hours": alert_h}

                # catch-up if already passed within 12h window
                if trigger_utc <= now_utc and (now_utc - trigger_utc) <= dt.timedelta(hours=12):
                    await self._fire_direct(
                        follower_id=fid,
                        person_id=u.user_id,
                        person_username=u.username,
                        person_birth=(u.birth_day, u.birth_month, u.birth_year),
                        follower_tz=_as_int(fprof.get("tz"), 0),
                        meta=meta,
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
                        "follower_tz": _as_int(fprof.get("tz"), 0),
                        "meta": meta,
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
            # fallback for older ptb apis
            try:
                return list(getattr(jq, "scheduler").queue)  # type: ignore[attr-defined]
            except Exception:
                return []

    async def _daily_refresh_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await self.schedule_all(self._last_horizon)
        except Exception as e:
            self.log.exception("daily refresh failed: %s", e)

    # ------- message building helpers (localized) -------

    def _age_part(self, *, day: Optional[int], month: Optional[int], year: Optional[int], base_date: dt.date,
                  update=None, context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> str:
        # builds " (turns N)" or " (исполнится N)"; empty if year missing
        try:
            if year and day and month:
                nxt = _next_birthday_date(int(day), int(month), int(year), base_date)
                if nxt:
                    years = nxt.year - int(year)
                    return t("alert_age_part", update=update, context=context, n=years)
        except Exception:
            pass
        return ""

    def _prealert_text(
        self,
        *,
        uname: str,
        age_part: str,
        days_left: int,
        bday_date: dt.date,
        update=None,
        context: Optional[ContextTypes.DEFAULT_TYPE] = None,
    ) -> str:
        # date as dd-mm
        date_str = f"{bday_date.day:02d}-{bday_date.month:02d}"
        return t(
            "alert_in_days",
            update=update,
            context=context,
            name=uname,
            age=age_part,
            days=days_left,
            date=date_str,
        )

    def _today_text(
        self,
        *,
        uname: str,
        age_part: str,
        update=None,
        context: Optional[ContextTypes.DEFAULT_TYPE] = None,
    ) -> str:
        return t("alert_today", update=update, context=context, name=uname, age=age_part)

    # ------- senders -------

    async def _fire_direct(
        self,
        *,
        follower_id: int,
        person_id: int,
        person_username: Optional[str],
        person_birth: Tuple[Optional[int], Optional[int], Optional[int]],
        follower_tz: int,
        meta: Dict[str, object],
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
        # compute days_left in follower tz right now
        f_tz = _tz_from_offset(_as_int(fprof.get("tz"), follower_tz))
        now_f = dt.datetime.now(dt.timezone.utc).astimezone(f_tz)
        today_f = now_f.date()
        bday_next = _next_birthday_date(int(d or 1), int(m or 1), y, today_f) if d and m else None

        age_part = self._age_part(day=d, month=m, year=y, base_date=today_f)
        if bday_next:
            days_left = (bday_next - today_f).days
            if days_left <= 0:
                msg = self._today_text(uname=uname, age_part=age_part)
            else:
                msg = self._prealert_text(uname=uname, age_part=age_part, days_left=days_left, bday_date=bday_next)
        else:
            # fallback to simple today-style
            msg = self._today_text(uname=uname, age_part=age_part)

        try:
            await self.app.bot.send_message(chat_id=chat_id, text=f"🎂 {msg} 🎉")
        except Exception as e:
            self.log.exception("send failed: %s", e)

    async def _fire_one(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        data = context.job.data or {}
        person_id = data.get("person_id")
        follower_id = data.get("follower_id")

        fprof = await self.users.get_user(follower_id)
        if not fprof:
            return
        chat_id = fprof.get("chat_id")
        if not chat_id:
            return

        pprof = await self.users.get_user(person_id)
        uname = (pprof.get("username") if pprof else data.get("person_username")) or f"id:{person_id}"

        d, m, y = (data.get("person_birth") or (None, None, None))
        f_tz = _tz_from_offset(_as_int(fprof.get("tz"), data.get("follower_tz") or 0))
        now_f = dt.datetime.now(dt.timezone.utc).astimezone(f_tz)
        today_f = now_f.date()

        bday_next = _next_birthday_date(int(d or 1), int(m or 1), y, today_f) if d and m else None
        age_part = self._age_part(day=d, month=m, year=y, base_date=today_f)

        if bday_next:
            days_left = (bday_next - today_f).days
            if days_left <= 0:
                msg = self._today_text(uname=uname, age_part=age_part)
            else:
                msg = self._prealert_text(uname=uname, age_part=age_part, days_left=days_left, bday_date=bday_next)
        else:
            msg = self._today_text(uname=uname, age_part=age_part)

        try:
            await self.app.bot.send_message(chat_id=chat_id, text=f"🎂 {msg} 🎉")
        except Exception as e:
            self.log.exception("send failed: %s", e)

    # ------- followers resolution -------

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

    # clean shutdown hook from main
    async def shutdown(self) -> None:
        # nuke scheduled jobs so we don't double-send after restart
        jq = getattr(self.app, "job_queue", None)
        if not jq:
            return
        for j in self._iter_jobs():
            try:
                j.schedule_removal()
            except Exception:
                pass
