"""Time sources, injectable so budget, gateway and watcher tests can run on a fake clock."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta


class Clock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class FakeClock(Clock):
    """Manual clock: sleep() advances time instantly."""

    def __init__(self, start: datetime | None = None) -> None:
        self._mono = 1000.0
        self._start = start or datetime(2026, 10, 3, tzinfo=UTC)

    def monotonic(self) -> float:
        return self._mono

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._mono - 1000.0)

    def advance(self, seconds: float) -> None:
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self._mono += max(0.0, seconds)
        await asyncio.sleep(0)
