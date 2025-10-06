# tiny maintenance watcher for main bot
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import aiosqlite
from telegram.ext import Application, ContextTypes

from .. import config

log = logging.getLogger("maintenance")

@dataclass
class MaintenanceService:
    app: Application
    db_path: str

    _active: bool = False
    _mode: str = "soft"
    _paused_jobs_snapshot: list = None  # type: ignore

    def _open(self):
        return aiosqlite.connect(self.db_path)

    async def _read_flag(self) -> tuple[bool,str]:
        async with self._open() as db:
            await db.execute("""
              create table if not exists admin_state(
                key text primary key, value text, updated_at text default (datetime('now'))
              )
            """)
            await db.commit()
            db.row_factory = aiosqlite.Row
            cur = await db.execute("select value from admin_state where key='maintenance'")
            row = await cur.fetchone()
            val = (row["value"] if row else "off:soft") or "off:soft"
            parts = val.split(":")
            return parts[0] == "on", parts[1] if len(parts) > 1 else "soft"

    async def tick(self, context: ContextTypes.DEFAULT_TYPE):
        enabled, mode = await self._read_flag()
        if enabled and not self._active:
            await self._enter(mode)
        if (not enabled) and self._active:
            await self._exit()

    async def _enter(self, mode: str):
        self._active = True
        self._mode = mode or "soft"
        log.info("maintenance: entering (%s)", self._mode)

        # stash a flag for handlers to short-circuit politely
        self.app.bot_data["maintenance"] = True

        # stop job queue tasks (save snapshot of names)
        jq = getattr(self.app, "job_queue", None)
        if jq:
            self._paused_jobs_snapshot = list(jq.jobs())
            for j in self._paused_jobs_snapshot:
                try:
                    j.pause()
                except Exception:
                    try: j.schedule_removal()
                    except Exception: pass

        # hard mode: stop the application gracefully
        if self._mode == "hard":
            try:
                await self.app.stop()
            except Exception:
                pass

    async def _exit(self):
        log.info("maintenance: leaving")
        self._active = False
        self.app.bot_data["maintenance"] = False

        # resume scheduling window
        jq = getattr(self.app, "job_queue", None)
        if jq and self._paused_jobs_snapshot:
            # reschedule outside
            try:
                from .notif_service import NotifService
                users = self.app.bot_data.get("users_repo")
                groups = self.app.bot_data.get("groups_repo")
                friends = self.app.bot_data.get("friends_repo")
                notif: NotifService = self.app.bot_data.get("notif_service")  # type: ignore
                if notif:
                    await notif.schedule_all(getattr(config, "SCHEDULE_HORIZON_DAYS", 7))
                    await notif.schedule_daily_refresh(at_hour=3)
            except Exception as e:
                log.exception("maintenance: failed to reschedule notifications: %s", e)
        self._paused_jobs_snapshot = []

