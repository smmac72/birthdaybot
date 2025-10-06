from __future__ import annotations
import aiosqlite
from typing import List, Dict, Optional


class WishlistRepo:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _open(self):
        return aiosqlite.connect(self.db_path)

    async def _ensure_schema(self, db) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS wishlist_items(
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                title     TEXT NOT NULL,
                url       TEXT,
                price     TEXT
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_w_user ON wishlist_items(user_id)")

    async def list_for_user(self, user_id: int) -> List[Dict]:
        async with self._open() as db:
            db.row_factory = aiosqlite.Row
            await self._ensure_schema(db)
            cur = await db.execute(
                "SELECT id, title, url, price FROM wishlist_items WHERE user_id=? ORDER BY id ASC",
                (int(user_id),),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def add_item(self, user_id: int, *, title: str, url: Optional[str], price: Optional[str]) -> int:
        async with self._open() as db:
            await self._ensure_schema(db)
            cur = await db.execute(
                "INSERT INTO wishlist_items(user_id, title, url, price) VALUES(?,?,?,?)",
                (int(user_id), title, (url or None), (price or None)),
            )
            await db.commit()
            return int(cur.lastrowid or 0)

    async def delete_item(self, user_id: int, item_id: int) -> bool:
        async with self._open() as db:
            await self._ensure_schema(db)
            cur = await db.execute(
                "DELETE FROM wishlist_items WHERE user_id=? AND id=?",
                (int(user_id), int(item_id)),
            )
            await db.commit()
            return cur.rowcount > 0
