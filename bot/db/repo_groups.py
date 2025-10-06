from __future__ import annotations

import time
import uuid
from typing import Optional, Dict, List

import aiosqlite
import sqlite3


class GroupsRepo:
    def __init__(self, db_path: str):
        # sqlite file path
        self.db_path = db_path

    # internal

    def _open(self):
        # connection coroutine (do not await here)
        return aiosqlite.connect(self.db_path)

    # schema / migrations

    async def _ensure_schema(self, db: aiosqlite.Connection) -> None:
        # base tables
        await db.execute("""
            CREATE TABLE IF NOT EXISTS groups(
                group_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                code TEXT UNIQUE NOT NULL
                -- owner column normalized below
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS group_members(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL
                -- member columns normalized below
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_g_code ON groups(code)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_gm_group ON group_members(group_id)")

        # detect current gm schema
        cur = await db.execute("PRAGMA table_info(group_members)")
        gm_info = await cur.fetchall()
        gm_cols = {row[1] for row in gm_info}
        gm_notnull = {row[1]: bool(row[3]) for row in gm_info}  # notnull flag

        # if legacy column user_id exists or has notnull -> hard rebuild table
        # also rebuild if we miss new columns entirely
        legacy_needs_rebuild = (
            ("user_id" in gm_cols) or
            ("username" in gm_cols) or
            (("member_user_id" not in gm_cols) and ("member_username" not in gm_cols)) or
            gm_notnull.get("user_id", False)
        )
        if legacy_needs_rebuild:
            await self._rebuild_group_members(db, gm_cols)

        # add missing columns on new table (idempotent)
        cur = await db.execute("PRAGMA table_info(group_members)")
        gm_cols = {row[1] for row in await cur.fetchall()}
        if "member_user_id" not in gm_cols:
            await db.execute("ALTER TABLE group_members ADD COLUMN member_user_id INTEGER")
        if "member_username" not in gm_cols:
            await db.execute("ALTER TABLE group_members ADD COLUMN member_username TEXT")
        if "birth_day" not in gm_cols:
            await db.execute("ALTER TABLE group_members ADD COLUMN birth_day INTEGER")
        if "birth_month" not in gm_cols:
            await db.execute("ALTER TABLE group_members ADD COLUMN birth_month INTEGER")
        if "birth_year" not in gm_cols:
            await db.execute("ALTER TABLE group_members ADD COLUMN birth_year INTEGER")
        if "joined_at" not in gm_cols:
            await db.execute("ALTER TABLE group_members ADD COLUMN joined_at INTEGER NOT NULL DEFAULT 0")

        # backfill joined_at
        await db.execute("""
            UPDATE group_members
               SET joined_at = CASE WHEN joined_at IS NULL OR joined_at = 0 THEN ? ELSE joined_at END
        """, (int(time.time()),))

        # --- UNIQUE indexes for UPSERTs ---
        # NOTE: UPSERT target (ON CONFLICT(...)) НЕ работает по partial unique indexes в SQLite.
        # Поэтому добавляем ПОЛНЫЕ (без WHERE) уникальные индексы с новыми именами.
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gm_unique_uid_full
                      ON group_members(group_id, member_user_id)
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gm_unique_uname_full
                      ON group_members(group_id, member_username)
        """)

        # (Старые частичные индексы оставляем для совместимости/производительности; они не мешают.)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gm_unique_uid
                      ON group_members(group_id, member_user_id)
                   WHERE member_user_id IS NOT NULL
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gm_unique_uname
                      ON group_members(group_id, member_username)
                   WHERE member_username IS NOT NULL
        """)

        # normalize groups owner column
        cur = await db.execute("PRAGMA table_info(groups)")
        g_info = await cur.fetchall()
        g_cols = {row[1] for row in g_info}
        g_notnull = {row[1]: bool(row[3]) for row in g_info}

        need_rebuild_groups = "creator_id" in g_cols or ("creator_user_id" in g_cols and g_notnull.get("creator_user_id", False))
        if need_rebuild_groups:
            await db.execute("PRAGMA foreign_keys=OFF")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS groups_new(
                    group_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    code TEXT UNIQUE NOT NULL,
                    creator_user_id INTEGER
                )
            """)
            if "creator_id" in g_cols:
                await db.execute("""
                    INSERT INTO groups_new(group_id, name, code, creator_user_id)
                    SELECT group_id, name, code, creator_id FROM groups
                """)
            else:
                await db.execute("""
                    INSERT INTO groups_new(group_id, name, code, creator_user_id)
                    SELECT group_id, name, code, creator_user_id FROM groups
                """)
            await db.execute("DROP TABLE groups")
            await db.execute("ALTER TABLE groups_new RENAME TO groups")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_g_code ON groups(code)")
            await db.execute("PRAGMA foreign_keys=ON")

        # add owner column if missing
        cur = await db.execute("PRAGMA table_info(groups)")
        g_cols = {row[1] for row in await cur.fetchall()}
        if "creator_user_id" not in g_cols:
            await db.execute("ALTER TABLE groups ADD COLUMN creator_user_id INTEGER")

        # backfill owner from oldest registered member if null
        await db.execute("""
            UPDATE groups
               SET creator_user_id = (
                   SELECT gm.member_user_id
                     FROM group_members gm
                    WHERE gm.group_id = groups.group_id
                      AND gm.member_user_id IS NOT NULL
                 ORDER BY gm.joined_at ASC
                    LIMIT 1
               )
             WHERE creator_user_id IS NULL
        """)

        await db.commit()

    async def _rebuild_group_members(self, db: aiosqlite.Connection, gm_cols: set) -> None:
        # rebuild gm to normalized schema, mapping legacy columns when present
        await db.execute("PRAGMA foreign_keys=OFF")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS group_members_new(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                member_user_id INTEGER,
                member_username TEXT,
                birth_day INTEGER,
                birth_month INTEGER,
                birth_year INTEGER,
                joined_at INTEGER NOT NULL DEFAULT 0
            )
        """)

        # craft dynamic select mapping from legacy cols
        sel_user_id = "user_id" if "user_id" in gm_cols else "NULL"
        sel_username = "username" if "username" in gm_cols else "NULL"
        sel_bd = "birth_day" if "birth_day" in gm_cols else "NULL"
        sel_bm = "birth_month" if "birth_month" in gm_cols else "NULL"
        sel_by = "birth_year" if "birth_year" in gm_cols else "NULL"
        sel_joined = "joined_at" if "joined_at" in gm_cols else str(int(time.time()))

        await db.execute(f"""
            INSERT INTO group_members_new(group_id, member_user_id, member_username, birth_day, birth_month, birth_year, joined_at)
            SELECT group_id, {sel_user_id}, {sel_username}, {sel_bd}, {sel_bm}, {sel_by}, {sel_joined}
              FROM group_members
        """)

        await db.execute("DROP TABLE group_members")
        await db.execute("ALTER TABLE group_members_new RENAME TO group_members")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_gm_group ON group_members(group_id)")

        # Создаём ПОЛНЫЕ unique индексы (для UPSERT)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gm_unique_uid_full
                      ON group_members(group_id, member_user_id)
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gm_unique_uname_full
                      ON group_members(group_id, member_username)
        """)

        # Оставляем также частичные индексы
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gm_unique_uid
                      ON group_members(group_id, member_user_id)
                   WHERE member_user_id IS NOT NULL
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gm_unique_uname
                      ON group_members(group_id, member_username)
                   WHERE member_username IS NOT NULL
        """)
        await db.execute("PRAGMA foreign_keys=ON")

    # helpers

    async def _ensure_creator_member(self, db: aiosqlite.Connection, group_id: str, creator_user_id: int) -> None:
        # ensure owner present in gm; add with username if available
        cur = await db.execute(
            "SELECT 1 FROM group_members WHERE group_id=? AND member_user_id=?",
            (group_id, creator_user_id),
        )
        if await cur.fetchone():
            return

        username = None
        cur = await db.execute("SELECT username FROM users WHERE user_id=?", (creator_user_id,))
        row = await cur.fetchone()
        if row and row["username"]:
            username = row["username"]

        await db.execute(
            "INSERT INTO group_members(group_id, member_user_id, member_username, joined_at) VALUES(?,?,?,?)",
            (group_id, creator_user_id, username, int(time.time())),
        )

    # queries

    async def create_group(self, name: str, creator_user_id: int) -> tuple[str, str]:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)

            gid = str(uuid.uuid4())
            code = None

            # generate unique invite code
            for _ in range(12):
                candidate = uuid.uuid4().hex[:8]
                try:
                    await db.execute(
                        "INSERT INTO groups(group_id, name, code, creator_user_id) VALUES(?,?,?,?)",
                        (gid, name, candidate, creator_user_id),
                    )
                    code = candidate
                    break
                except sqlite3.IntegrityError as e:
                    if "UNIQUE constraint failed: groups.code" in str(e):
                        continue
                    raise
            if not code:
                raise RuntimeError("failed to generate unique group code")

            # add owner to members
            await self._ensure_creator_member(db, gid, creator_user_id)

            await db.commit()
            return gid, code

    async def get_by_code(self, code: str) -> Optional[Dict[str, any]]:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)
            cur = await db.execute("SELECT * FROM groups WHERE code=?", (code,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_user_groups(self, user_id: int) -> List[Dict[str, any]]:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)
            cur = await db.execute(
                """
                SELECT g.group_id,
                       g.name,
                       g.code,
                       g.creator_user_id,
                       COALESCE(COUNT(m.id), 0) AS member_count
                  FROM groups g
             LEFT JOIN group_members m ON m.group_id = g.group_id
                 WHERE g.group_id IN (SELECT group_id FROM group_members WHERE member_user_id = ?)
                    OR g.creator_user_id = ?
              GROUP BY g.group_id
              ORDER BY g.name COLLATE NOCASE
                """,
                (user_id, user_id),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def join_by_code(self, code: str, user_id: int) -> tuple[bool, Optional[str]]:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)

            cur = await db.execute("SELECT group_id, name FROM groups WHERE code=?", (code,))
            g = await cur.fetchone()
            if not g:
                return False, None
            gid, gname = g["group_id"], g["name"]

            cur = await db.execute(
                "SELECT 1 FROM group_members WHERE group_id=? AND member_user_id=?",
                (gid, user_id),
            )
            if await cur.fetchone():
                return False, gname

            await db.execute(
                "INSERT INTO group_members(group_id, member_user_id, joined_at) VALUES(?,?,?)",
                (gid, user_id, int(time.time())),
            )
            await db.commit()
            return True, gname

    async def leave_by_code(self, code: str, user_id: int) -> tuple[bool, Optional[str]]:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)

            cur = await db.execute("SELECT * FROM groups WHERE code=?", (code,))
            g = await cur.fetchone()
            if not g:
                return False, None

            gid = g["group_id"]
            gname = g["name"]
            creator_id = g["creator_user_id"]

            # try to delete membership if exists
            cur = await db.execute(
                "SELECT id FROM group_members WHERE group_id=? AND member_user_id=?",
                (gid, user_id),
            )
            mem = await cur.fetchone()
            if mem:
                await db.execute(
                    "DELETE FROM group_members WHERE group_id=? AND member_user_id=?",
                    (gid, user_id),
                )

            if user_id != creator_id:
                await db.commit()
                return bool(mem), gname

            # owner leaving: transfer or dissolve
            cur = await db.execute(
                """
                SELECT m.member_user_id AS uid
                  FROM group_members m
                 WHERE m.group_id=? AND m.member_user_id IS NOT NULL
              ORDER BY m.joined_at ASC
                 LIMIT 1
                """,
                (gid,),
            )
            new_owner = await cur.fetchone()

            if new_owner and new_owner["uid"]:
                await db.execute(
                    "UPDATE groups SET creator_user_id=? WHERE group_id=?",
                    (int(new_owner["uid"]), gid),
                )
                await db.commit()
                return True, gname

            # no registered members remain -> dissolve
            await db.execute("DELETE FROM group_members WHERE group_id=?", (gid,))
            await db.execute("DELETE FROM groups WHERE group_id=?", (gid,))
            await db.commit()
            return True, gname

    async def rename_group(self, group_id: str, new_name: str) -> None:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)
            await db.execute("UPDATE groups SET name=? WHERE group_id=?", (new_name, group_id))
            await db.commit()

    async def add_member(
        self,
        group_id: str,
        user_id: Optional[int],
        username: Optional[str],
        birth_day: Optional[int],
        birth_month: Optional[int],
        birth_year: Optional[int],
    ) -> None:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)

            # prefer canonical profile if registered
            if user_id:
                cur = await db.execute(
                    "SELECT username, birth_day, birth_month, birth_year FROM users WHERE user_id=?",
                    (user_id,),
                )
                u = await cur.fetchone()
                if u:
                    if not username:
                        username = u["username"]
                    if not birth_day and u["birth_day"]:
                        birth_day = u["birth_day"]
                        birth_month = u["birth_month"]
                        birth_year = u["birth_year"]

            ts = int(time.time())
            if user_id is not None:
                await db.execute(
                    """
                    INSERT INTO group_members(group_id, member_user_id, member_username, birth_day, birth_month, birth_year, joined_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(group_id, member_user_id) DO UPDATE SET
                        member_username=excluded.member_username,
                        birth_day=COALESCE(group_members.birth_day, excluded.birth_day),
                        birth_month=COALESCE(group_members.birth_month, excluded.birth_month),
                        birth_year=COALESCE(group_members.birth_year, excluded.birth_year)
                    """,
                    (group_id, user_id, username, birth_day, birth_month, birth_year, ts),
                )
            elif username:
                await db.execute(
                    """
                    INSERT INTO group_members(group_id, member_user_id, member_username, birth_day, birth_month, birth_year, joined_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(group_id, member_username) DO UPDATE SET
                        birth_day=COALESCE(group_members.birth_day, excluded.birth_day),
                        birth_month=COALESCE(group_members.birth_month, excluded.birth_month),
                        birth_year=COALESCE(group_members.birth_year, excluded.birth_year)
                    """,
                    (group_id, None, username, birth_day, birth_month, birth_year, ts),
                )
            else:
                await db.commit()
                return

            await db.commit()

    async def remove_member(
        self,
        group_id: str,
        target_user_id: Optional[int] = None,
        username: Optional[str] = None,
    ) -> bool:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)

            if target_user_id is not None:
                cur = await db.execute(
                    "DELETE FROM group_members WHERE group_id=? AND member_user_id=?",
                    (group_id, target_user_id),
                )
            elif username:
                cur = await db.execute(
                    "DELETE FROM group_members WHERE group_id=? AND member_username=?",
                    (group_id, username),
                )
            else:
                await db.commit()
                return False
            await db.commit()
            return cur.rowcount > 0  # type: ignore

    async def list_members(self, group_id: str) -> List[Dict[str, any]]:
        async with self._open() as db:
            db.row_factory = sqlite3.Row
            await self._ensure_schema(db)

            cur = await db.execute(
                """
                SELECT
                    COALESCE(m.member_user_id, u.user_id) AS user_id,
                    COALESCE(u.username, m.member_username) AS username,
                    COALESCE(u.birth_day, m.birth_day)     AS birth_day,
                    COALESCE(u.birth_month, m.birth_month) AS birth_month,
                    COALESCE(u.birth_year, m.birth_year)   AS birth_year
                  FROM group_members m
             LEFT JOIN users u ON u.user_id = m.member_user_id
                 WHERE m.group_id=?
                """,
                (group_id,),
            )
            rows = [dict(r) for r in await cur.fetchall()]

            # ensure owner visible even if not in gm
            cur = await db.execute("SELECT creator_user_id FROM groups WHERE group_id=?", (group_id,))
            g = await cur.fetchone()
            owner_id = g["creator_user_id"] if g else None
            owner_present = owner_id is not None and any(r.get("user_id") == owner_id for r in rows)

            if owner_id is not None and not owner_present:
                cur = await db.execute(
                    "SELECT user_id, username, birth_day, birth_month, birth_year FROM users WHERE user_id=?",
                    (owner_id,),
                )
                u = await cur.fetchone()
                if u:
                    rows.append(dict(u))
                else:
                    rows.append(dict(user_id=owner_id, username=None, birth_day=None, birth_month=None, birth_year=None))

            return rows
