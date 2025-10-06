from __future__ import annotations

# tiny async sqlite layer for admin bot

import json
import aiosqlite
from typing import List, Dict, Any


class AdminRepo:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _open(self):
        return aiosqlite.connect(self.db_path)

    # --- schema ---

    async def reset_schema(self) -> None:
        # nuke-and-pave: drop and recreate only what admin bot needs
        async with self._open() as db:
            await db.execute("drop table if exists admin_state")
            await db.execute("drop table if exists admin_events")
            await db.execute("drop table if exists error_logs")
            await db.commit()
        await self.ensure_schema()

    async def ensure_schema(self) -> None:
        # fresh, simple schema (no migrations)
        async with self._open() as db:
            # admin_state
            await db.execute("""
              create table if not exists admin_state(
                key text primary key,
                value text,
                updated_at text default (datetime('now'))
              )
            """)

            # error_logs (read-only for admin)
            await db.execute("""
              create table if not exists error_logs(
                id integer primary key autoincrement,
                ts text not null,
                level text not null,
                source text,
                message text
              )
            """)

            # admin_events — but make sure it's the fresh shape
            await self._ensure_events_table_fresh(db)

            await db.commit()

    async def _ensure_events_table_fresh(self, db: aiosqlite.Connection) -> None:
        # check existing columns; if legacy shape -> drop and recreate
        await db.execute("""
          create table if not exists admin_events(
            id integer primary key autoincrement,
            kind text not null,
            payload text,
            created_at text default (datetime('now')),
            processed integer not null default 0
          )
        """)
        # inspect columns
        cur = await db.execute("pragma table_info('admin_events')")
        cols = [r[1] for r in await cur.fetchall()]  # r[1] == name
        need_recreate = False
        # legacy had 'ts' (not null) instead of 'created_at'
        if "ts" in cols:
            need_recreate = True
        # current must have these
        for must in ("id", "kind", "payload", "created_at", "processed"):
            if must not in cols:
                need_recreate = True
                break
        if need_recreate:
            await db.execute("drop table if exists admin_events")
            await db.execute("""
              create table admin_events(
                id integer primary key autoincrement,
                kind text not null,
                payload text,
                created_at text default (datetime('now')),
                processed integer not null default 0
              )
            """)

    # --- broadcast target list ---

    async def list_all_chat_ids(self) -> List[int]:
        # fallback to user_id when chat_id missing
        async with self._open() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("select distinct coalesce(chat_id, user_id) as cid from users")
            rows = await cur.fetchall()
            return [int(r["cid"]) for r in rows if r and r["cid"] is not None]

    # --- maintenance flag ---

    async def set_maintenance(self, *, enabled: bool, mode: str = "soft") -> None:
        await self.ensure_schema()
        val = f"{'on' if enabled else 'off'}:{mode}"
        async with self._open() as db:
            await db.execute(
                "insert into admin_state(key, value) values('maintenance', ?) "
                "on conflict(key) do update set value=excluded.value, updated_at=datetime('now')",
                (val,),
            )
            await db.commit()

    async def get_maintenance(self) -> Dict[str, Any]:
        await self.ensure_schema()
        async with self._open() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("select value from admin_state where key='maintenance'")
            row = await cur.fetchone()
            val = (row["value"] if row else "off:soft") or "off:soft"
            parts = val.split(":")
            return {"enabled": parts[0] == "on", "mode": parts[1] if len(parts) > 1 else "soft"}

    # --- event queue (admin -> main) ---

    async def enqueue_event(self, kind: str, payload: Dict[str, Any]) -> int:
        await self.ensure_schema()
        async with self._open() as db:
            cur = await db.execute(
                "insert into admin_events(kind, payload) values(?, ?)",
                (kind, json.dumps(payload, ensure_ascii=False)),
            )
            await db.commit()
            return int(cur.lastrowid or 0)

    async def fetch_pending_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        await self.ensure_schema()
        async with self._open() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "select id, kind, payload from admin_events where processed=0 order by id asc limit ?",
                (int(limit),),
            )
            rows = await cur.fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                payload = {}
                try:
                    payload = json.loads(r["payload"] or "{}")
                except Exception:
                    pass
                out.append({"id": int(r["id"]), "kind": r["kind"], "payload": payload})
            return out

    async def mark_events_processed(self, ids: List[int]) -> None:
        if not ids:
            return
        q = "update admin_events set processed=1 where id in (%s)" % ",".join("?" * len(ids))
        async with self._open() as db:
            await db.execute(q, [int(i) for i in ids])
            await db.commit()

    # --- lightweight analytics ---

    async def stats_summary(self) -> Dict[str, Any]:
        async with self._open() as db:
            db.row_factory = aiosqlite.Row
            out: Dict[str, Any] = {}
            out["users_total"] = int((await (await db.execute("select count(*) c from users")).fetchone())["c"])
            out["users_with_bday"] = int((await (await db.execute("select count(*) c from users where birth_day is not null and birth_month is not null")).fetchone())["c"])
            out["groups_total"] = int((await (await db.execute("select count(*) c from groups")).fetchone())["c"])
            out["friends_total"] = int((await (await db.execute("select count(*) c from friends")).fetchone())["c"])
            out["notif_30d"] = 0
            return out

    async def top_groups(self) -> List[Dict[str, Any]]:
        async with self._open() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
              select g.name, g.code, count(m.id) as members
              from groups g left join group_members m on m.group_id=g.group_id
              group by g.group_id
              order by members desc, g.name collate nocase
              limit 10
            """)
            return [dict(r) for r in await cur.fetchall()]

    async def top_users_followed(self) -> List[Dict[str, Any]]:
        async with self._open() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
              with f as (
                select friend_user_id as uid from friends where friend_user_id is not null
                union all
                select gm.member_user_id as uid from group_members gm where gm.member_user_id is not null
              )
              select u.user_id, u.username, count(*) as total
              from users u
              join f on f.uid = u.user_id
              group by u.user_id
              order by total desc
              limit 10
            """)
            return [dict(r) for r in await cur.fetchall()]

    async def errors_recent(self, n: int = 20) -> List[Dict[str, Any]]:
        async with self._open() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("select * from error_logs order by id desc limit ?", (int(n),))
            return [dict(r) for r in await cur.fetchall()]
