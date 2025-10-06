# tiny helper to log + optionally push errors to admins from the main bot
from __future__ import annotations

import json
import time
import aiosqlite
from typing import Any, Optional

from .. import config


async def admin_notify(*, db_path: str, application, level: str, source: str, message: str, data: Optional[dict] = None) -> None:
    # insert into admin_events
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS admin_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    source TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data TEXT
                )
            """)
            await db.execute(
                "INSERT INTO admin_events(ts, level, source, message, data) VALUES(?,?,?,?,?)",
                (int(time.time()), level, source, message, json.dumps(data or {})[:4000]),
            )
            await db.commit()
    except Exception:
        # swallow db issues
        pass

    # send to admin chat via main bot
    chat_id = getattr(config, "ADMIN_CHAT_ID", 0) or 0
    if not chat_id:
        return
    try:
        text = f"[{level.upper()}] {source}\n{message}"
        await application.bot.send_message(chat_id=chat_id, text=text[:4000])
    except Exception:
        pass
