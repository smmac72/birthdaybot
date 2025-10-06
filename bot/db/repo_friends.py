from __future__ import annotations
import aiosqlite
from typing import Optional, List, Dict, Any

class FriendsRepo:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def _ensure_schema(self, db) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS friends(
                owner_user_id    INTEGER NOT NULL,
                friend_user_id   INTEGER,
                friend_username  TEXT,
                birth_day        INTEGER,
                birth_month      INTEGER,
                birth_year       INTEGER,
                PRIMARY KEY(owner_user_id, friend_user_id, friend_username)
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_f_owner ON friends(owner_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_f_friend_id ON friends(friend_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_f_friend_un ON friends(LOWER(friend_username))")

    # ------- public api -------
    async def list_for_user(self, owner_user_id: int) -> List[Dict[str, Any]]:
        """
        returns user's friends
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await self._ensure_schema(db)
            cur = await db.execute(
                """
                SELECT
                    f.owner_user_id,
                    f.friend_user_id,
                    COALESCE(u.username, f.friend_username)    AS friend_username,
                    COALESCE(u.birth_day,   f.birth_day)        AS birth_day,
                    COALESCE(u.birth_month, f.birth_month)      AS birth_month,
                    COALESCE(u.birth_year,  f.birth_year)       AS birth_year
                FROM friends f
                LEFT JOIN users u
                       ON u.user_id = f.friend_user_id
                WHERE f.owner_user_id = ?
                """,
                (owner_user_id,),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def add_friend(
        self,
        owner_user_id: int,
        *,
        friend_user_id: Optional[int] = None,
        friend_username: Optional[str] = None,
        birth_day: Optional[int] = None,
        birth_month: Optional[int] = None,
        birth_year: Optional[int] = None,
    ) -> None:
        fu = friend_username or None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await self._ensure_schema(db)
            await db.execute(
                """
                INSERT OR REPLACE INTO friends
                    (owner_user_id, friend_user_id, friend_username, birth_day, birth_month, birth_year)
                VALUES(?,?,?,?,?,?)
                """,
                (owner_user_id, friend_user_id, fu, birth_day, birth_month, birth_year),
            )
            await db.commit()

    async def delete_friend(
        self,
        owner_user_id: int,
        *,
        friend_user_id: Optional[int] = None,
        friend_username: Optional[str] = None,
    ) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await self._ensure_schema(db)
            if friend_user_id is not None:
                cur = await db.execute(
                    "DELETE FROM friends WHERE owner_user_id=? AND friend_user_id=?",
                    (owner_user_id, friend_user_id),
                )
            else:
                cur = await db.execute(
                    "DELETE FROM friends WHERE owner_user_id=? AND LOWER(friend_username)=LOWER(?)",
                    (owner_user_id, friend_username or ""),
                )
            await db.commit()
            return cur.rowcount > 0

    async def count_followers(self, *, user_id: Optional[int], username_lower: Optional[str]) -> int:
        """
        how many owners track this person (by id or username match when id is null)
        """
        total = 0
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await self._ensure_schema(db)
            if user_id is not None:
                cur = await db.execute(
                    "SELECT COUNT(DISTINCT owner_user_id) FROM friends WHERE friend_user_id=?",
                    (user_id,),
                )
                row = await cur.fetchone()
                total += int((row or (0,))[0] or 0)
            if username_lower:
                cur = await db.execute(
                    "SELECT COUNT(DISTINCT owner_user_id) FROM friends WHERE friend_user_id IS NULL AND LOWER(friend_username)=?",
                    (username_lower,),
                )
                row = await cur.fetchone()
                total += int((row or (0,))[0] or 0)
        return int(total)

    async def list_owners_for_person(
        self,
        *,
        person_user_id: Optional[int],
        username_lower: Optional[str],
    ) -> List[int]:
        # owners who track this person by id or username
        owners: List[int] = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await self._ensure_schema(db)
            if person_user_id is not None:
                cur = await db.execute(
                    "SELECT DISTINCT owner_user_id FROM friends WHERE friend_user_id=?",
                    (person_user_id,),
                )
                owners += [int(r[0]) for r in await cur.fetchall() if r and r[0] is not None]
            if username_lower:
                cur = await db.execute(
                    "SELECT DISTINCT owner_user_id FROM friends WHERE friend_user_id IS NULL AND LOWER(friend_username)=?",
                    (username_lower,),
                )
                owners += [int(r[0]) for r in await cur.fetchall() if r and r[0] is not None]
        # unique
        return list({o for o in owners if o})
