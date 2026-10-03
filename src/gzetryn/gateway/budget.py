"""One request budget for every GMGN call leaving the VPS (spec §10).

Token bucket (`per_minute` refill, `burst` capacity) plus a minimum gap between request starts. P0 may take the
last token; P1 leaves `reserve_p1` tokens for P0, P2 leaves `reserve_p2`. A 429/403 pauses everything: 120 s,
doubling up to 30 min, back to the first step after 30 min without a throttle.
"""

from __future__ import annotations

from gzetryn.clock import Clock
from gzetryn.config import BudgetTunables

PRIORITIES = ("P0", "P1", "P2")


class Budget:
    def __init__(self, tunables: BudgetTunables, clock: Clock | None = None):
        self._t = tunables
        self._clock = clock or Clock()
        self._capacity = float(tunables.burst)
        self._rate = tunables.per_minute / 60.0
        self._tokens = self._capacity
        self._stamp = self._clock.monotonic()
        self._last_start = -1e9
        self._paused_until = 0.0
        self._pause_step = tunables.throttle_pause_sec
        self._last_throttle = -1e9
        self.throttles = 0
        self.granted = {p: 0 for p in PRIORITIES}
        self.denied = {p: 0 for p in PRIORITIES}

    def _reserve(self, priority: str) -> float:
        return {"P0": 0.0, "P1": self._t.reserve_p1}.get(priority, self._t.reserve_p2)

    def _refill(self) -> None:
        now = self._clock.monotonic()
        self._tokens = min(self._capacity, self._tokens + (now - self._stamp) * self._rate)
        self._stamp = now

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def paused_for(self) -> float:
        return max(0.0, self._paused_until - self._clock.monotonic())

    def max_wait(self, priority: str) -> float:
        return self._t.max_wait_sec.get(priority, 60.0)

    async def acquire(self, priority: str, max_wait_sec: float | None = None) -> bool:
        wait_limit = self.max_wait(priority) if max_wait_sec is None else max_wait_sec
        deadline = self._clock.monotonic() + wait_limit
        need = 1.0 + self._reserve(priority)
        while True:
            self._refill()
            now = self._clock.monotonic()
            gap_left = self._last_start + self._t.min_gap_sec - now
            if now >= self._paused_until and self._tokens >= need and gap_left <= 0:
                self._tokens -= 1.0
                self._last_start = now
                self.granted[priority] = self.granted.get(priority, 0) + 1
                return True
            wait = max(self._paused_until - now, (need - self._tokens) / self._rate, gap_left, 0.02)
            if now + wait > deadline:
                self.denied[priority] = self.denied.get(priority, 0) + 1
                return False
            await self._clock.sleep(min(wait, 0.5))

    def throttle(self) -> float:
        """Record a 429/403; returns the pause in seconds."""
        now = self._clock.monotonic()
        if now - self._last_throttle > self._t.throttle_reset_sec:
            self._pause_step = self._t.throttle_pause_sec
        pause = self._pause_step
        self._paused_until = max(self._paused_until, now + pause)
        self._pause_step = min(self._pause_step * 2, self._t.throttle_pause_max_sec)
        self._last_throttle = now
        self.throttles += 1
        return pause
