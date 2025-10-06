from __future__ import annotations

import sqlite3
import aiosqlite
from typing import Optional, Dict, Any, List


class UsersRepo:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lang_cache: dict[int, str] = {}

    # always open fresh connection (avoids "threads can only be started once")
    def _open(self):
        return aiosqlite.connect(self.db_path)

    async def _ensure_schema(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """
            create table if not exists users (
                user_id     integer primary key,
                username    text,
                chat_id     integer,
                birth_day   integer,
                birth_month integer,
                birth_year  integer,
                tz          integer not null default 0,
                alert_hours integer not null default 0,
                lang        text default 'ru',
                created_at  text default (datetime('now'))
            )
            """
        )
        # backfill lang if column just added
        try:
            await db.execute("alter table users add column lang text default 'ru'")
        except Exception:
            pass
        await db.execute("create index if not exists idx_users_username on users(username)")
        await db.execute("create index if not exists idx_users_chat on users(chat_id)")
        await db.commit()

    @staticmethod
    def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        return {k: row[k] for k in row.keys()}

    # create or update user

    async def ensure_user(self, tg_user, chat_id: Optional[int] = None) -> Dict[str, Any]:
        uid = int(tg_user.id)
        uname = tg_user.username or None

        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)

            await db.execute(
                "insert or ignore into users(user_id, username) values(?, ?)",
                (uid, uname),
            )
            await db.execute("update users set username = ? where user_id = ?", (uname, uid))
            if chat_id is not None:
                await db.execute("update users set chat_id = ? where user_id = ?", (int(chat_id), uid))

            await db.commit()

            cur = await db.execute("select * from users where user_id = ?", (uid,))
            row = await cur.fetchone()
            d = self._row_to_dict(row) or {}
            # cache lang
            if d.get("lang"):
                try:
                    self._lang_cache[uid] = str(d["lang"])
                except Exception:
                    pass
            return d

    # updates

    async def update_chat_id(self, user_id: int, chat_id: int) -> None:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)
            await db.execute("update users set chat_id = ? where user_id = ?", (int(chat_id), int(user_id)))
            await db.commit()

    async def update_bday(
        self, user_id: int, birth_day: Optional[int], birth_month: Optional[int], birth_year: Optional[int]
    ) -> None:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)
            await db.execute(
                "update users set birth_day = ?, birth_month = ?, birth_year = ? where user_id = ?",
                (birth_day, birth_month, birth_year, int(user_id)),
            )
            await db.commit()

    async def update_tz(self, user_id: int, tz_hours: int) -> None:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)
            await db.execute("update users set tz = ? where user_id = ?", (int(tz_hours), int(user_id)))
            await db.commit()

    async def update_alert_hours(self, user_id: int, hours: int) -> None:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)
            await db.execute("update users set alert_hours = ? where user_id = ?", (int(hours), int(user_id)))
            await db.commit()

    async def set_username(self, user_id: int, username: Optional[str]) -> None:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)
            await db.execute("update users set username = ? where user_id = ?", (username, int(user_id)))
            await db.commit()

    async def set_lang(self, user_id: int, lang: str) -> None:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)
            await db.execute("update users set lang = ? where user_id = ?", (lang, int(user_id)))
            await db.commit()
        # cache
        self._lang_cache[user_id] = lang

    def get_cached_lang(self, user_id: int) -> Optional[str]:
        return self._lang_cache.get(user_id)

    # reads

    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)
            cur = await db.execute("select * from users where user_id = ?", (int(user_id),))
            row = await cur.fetchone()
            d = self._row_to_dict(row)
            if d and d.get("lang"):
                self._lang_cache[int(user_id)] = str(d["lang"])
            return d

    async def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)
            cur = await db.execute(
                "select * from users where lower(username) = lower(?) limit 1",
                (username,),
            )
            row = await cur.fetchone()
            d = self._row_to_dict(row)
            if d and d.get("lang") and d.get("user_id"):
                self._lang_cache[int(d["user_id"])] = str(d["lang"])
            return d

    # backward compat alias
    async def get_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        return await self.get_user_by_username(username)

    # batches for notif service

    async def list_all_users_with_bday(self) -> List[Dict[str, Any]]:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)
            cur = await db.execute(
                """
                select user_id, username, chat_id, birth_day, birth_month, birth_year, tz, alert_hours, lang
                from users
                where birth_day is not null and birth_month is not null
                """
            )
            rows = await cur.fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = self._row_to_dict(r) or {}
                # sanitize ints
                try:
                    d["tz"] = int(d.get("tz", 0))
                except Exception:
                    d["tz"] = 0
                try:
                    d["alert_hours"] = int(d.get("alert_hours", 0))
                except Exception:
                    d["alert_hours"] = 0
                out.append(d)
            return out

    async def list_all_user_ids(self) -> List[int]:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)
            cur = await db.execute("select user_id from users")
            rows = await cur.fetchall()
            return [int(r["user_id"]) for r in rows if r and r["user_id"] is not None]
