"""launch_paths store (phase 15): finalized curve paths, late graduation updates, retention."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gzetryn.store.models import LaunchPath


class LaunchPathStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def insert(self, rows: list[dict]) -> int:
        """Insert finalized paths (a mint already stored is left as it is); returns the rows written."""
        if not rows:
            return 0
        async with self._sessions() as s, s.begin():
            res = await s.execute(insert(LaunchPath).values(rows).on_conflict_do_nothing(index_elements=["mint"]))
            return res.rowcount or 0

    async def mark_completed(self, mint: str, completed_at: datetime | None, pool: str | None) -> int:
        """A graduation after the window: set completed_at / migrated_pool on the stored path (first value wins)."""
        values: dict = {}
        if completed_at is not None:
            values["completed_at"] = func.coalesce(LaunchPath.completed_at, completed_at)
        if pool is not None:
            values["migrated_pool"] = func.coalesce(LaunchPath.migrated_pool, pool)
        if not values:
            return 0
        async with self._sessions() as s, s.begin():
            res = await s.execute(update(LaunchPath).where(LaunchPath.mint == mint).values(**values))
            return res.rowcount or 0

    async def get(self, mint: str) -> LaunchPath | None:
        async with self._sessions() as s:
            return (await s.execute(select(LaunchPath).where(LaunchPath.mint == mint))).scalar_one_or_none()

    async def retention(self, days: int, now: datetime) -> int:
        async with self._sessions() as s, s.begin():
            res = await s.execute(delete(LaunchPath).where(LaunchPath.created_at < now - timedelta(days=days)))
            return res.rowcount or 0
