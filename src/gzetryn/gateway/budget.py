"""Request budget for every GMGN call leaving the VPS (spec §10, revised 2026-10-05).

Facts (phase 8 report): Cloudflare answers `429` + `cf-mitigated: challenge` per **path group** — `/vas/` was
challenged for ~7 min while `/api/` and `/defi/` answered 200. So:

- each group (`vas`, `api`, `defi`, `mrwapi`, by the first path segment) has its own token bucket, minimum gap and
  **cooldown** after a throttle: 15 s, doubling to 5 min, reset after 15 min clean. After a cooldown exactly one
  request (a probe) goes through; the group reopens only when it succeeds.
- a **global** pause (60 s) happens only when 2+ groups are cooling at the same time.
- a global token bucket keeps the overall cap.
- **strict priority** inside a group (D-2026-10-05-14): P0 (engine token intel) > P1 (other API, chain-event
  enrichment) > P2 (pump lists) > P3 (wallet_activity sweep, background); a lower priority never takes a token while
  a higher one waits in the same group, and a group may keep tokens in reserve for the higher ones (`vas`: P2/P3
  leave 4, one survivor's intel). P0 may use shorter gaps (`p0_min_gap_sec`, `p0_global_gap_sec`).
- sliding windows of request starts per group are kept, so the rate GMGN tolerates can be measured (peak count in
  any 10 s / 60 s window, and the counts just before each throttle).
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field

from gzetryn.clock import Clock
from gzetryn.config import BudgetTunables, GroupTunables

PRIORITIES = ("P0", "P1", "P2", "P3")
WINDOWS = (10.0, 60.0, 300.0)


class Denied(Exception):
    def __init__(self, reason: str, retry_after: float):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


@dataclass
class Bucket:
    rate: float  # tokens per second
    capacity: float
    tokens: float
    stamp: float

    def refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now


@dataclass
class GroupState:
    name: str
    t: GroupTunables
    bucket: Bucket
    last_start: float = -1e9
    cooling_until: float = 0.0
    step: float = 0.0
    last_throttle: float = -1e9
    probing: bool = False
    probe_inflight: bool = False
    level: int = 0  # ladder level: +1 per throttle, -1 per clean cooldown_decay_sec
    last_decay: float = -1e9
    reopened_at: float = -1e9  # last probe success (half rate for reopen_slow_sec afterwards)
    waiting: Counter = field(default_factory=Counter)
    starts: deque = field(default_factory=lambda: deque(maxlen=5000))
    throttles: int = 0
    cooldown_sec_total: float = 0.0
    peak: dict = field(default_factory=lambda: {int(w): 0 for w in WINDOWS})
    last_throttle_windows: dict | None = None
    granted: Counter = field(default_factory=Counter)
    denied: Counter = field(default_factory=Counter)


class Budget:
    def __init__(self, tunables: BudgetTunables, clock: Clock | None = None):
        self._t = tunables
        self._clock = clock or Clock()
        now = self._clock.monotonic()
        self._global = Bucket(tunables.per_minute / 60.0, float(tunables.burst), float(tunables.burst), now)
        self._global_last = -1e9
        self._global_paused_until = 0.0
        self.global_pauses = 0
        self._groups: dict[str, GroupState] = {}
        for name in tunables.groups:
            self._group(name)

    def _group(self, name: str) -> GroupState:
        g = self._groups.get(name)
        if g is None:
            gt = self._t.groups.get(name) or GroupTunables()
            now = self._clock.monotonic()
            g = GroupState(name, gt, Bucket(gt.per_minute / 60.0, gt.burst, gt.burst, now))
            self._groups[name] = g
        return g

    # ---------- reporting ----------

    @property
    def throttles(self) -> int:
        return sum(g.throttles for g in self._groups.values())

    @property
    def tokens(self) -> float:
        self._global.refill(self._clock.monotonic())
        return self._global.tokens

    @property
    def granted(self) -> dict[str, int]:
        c: Counter = Counter()
        for g in self._groups.values():
            c.update(g.granted)
        return {p: c.get(p, 0) for p in PRIORITIES}

    @property
    def denied(self) -> dict[str, int]:
        c: Counter = Counter()
        for g in self._groups.values():
            c.update(g.denied)
        return {p: c.get(p, 0) for p in PRIORITIES}

    def paused_for(self, group: str | None = None) -> float:
        now = self._clock.monotonic()
        left = max(0.0, self._global_paused_until - now)
        if group is not None:
            left = max(left, self._groups[group].cooling_until - now) if group in self._groups else left
        return left

    def is_open(self, group: str) -> bool:
        """The group serves requests normally: not cooling, not waiting for a probe, no global pause."""
        now = self._clock.monotonic()
        g = self._groups.get(group)
        if now < self._global_paused_until:
            return False
        return g is None or (now >= g.cooling_until and not g.probing)

    def cooling(self) -> dict[str, float]:
        now = self._clock.monotonic()
        return {n: round(g.cooling_until - now, 1) for n, g in self._groups.items() if g.cooling_until > now}

    def windows(self, g: GroupState) -> dict[int, int]:
        now = self._clock.monotonic()
        return {int(w): sum(1 for s in g.starts if now - s <= w) for w in WINDOWS}

    def report(self) -> dict:
        now = self._clock.monotonic()
        out = {}
        for n, g in self._groups.items():
            g.bucket.refill(now)
            self._decay(g, now)
            out[n] = {
                "slow_after_reopen_sec": round(max(0.0, self._t.reopen_slow_sec - (now - g.reopened_at)), 1),
                "per_minute": g.t.per_minute,
                "burst": g.t.burst,
                "min_gap_sec": g.t.min_gap_sec,
                "tokens": round(g.bucket.tokens, 2),
                "cooling_sec": round(max(0.0, g.cooling_until - now), 1),
                "cooldown_step_sec": g.step,
                "cooldown_level": g.level,
                "probing": g.probing,
                "throttles": g.throttles,
                "cooldown_sec_total": round(g.cooldown_sec_total, 1),
                "now": self.windows(g),
                "peak": dict(g.peak),
                "last_throttle_windows": g.last_throttle_windows,
                "granted": {p: g.granted.get(p, 0) for p in PRIORITIES},
                "denied": {p: g.denied.get(p, 0) for p in PRIORITIES},
            }
        return out

    # ---------- acquire / outcome ----------

    def _higher_waiting(self, g: GroupState, priority: str) -> bool:
        rank = PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES)
        return any(g.waiting[p] > 0 for p in PRIORITIES[:rank])

    async def acquire(self, priority: str, max_wait_sec: float | None = None, group: str = "other") -> bool:
        try:
            await self.take(priority, group, max_wait_sec)
            return True
        except Denied:
            return False

    async def take(self, priority: str, group: str, max_wait_sec: float | None = None) -> None:
        """Wait for a slot; raises Denied(reason, retry_after) when it cannot be had within the wait limit."""
        g = self._group(group)
        limit = self._t.max_wait_sec.get(priority, 60.0) if max_wait_sec is None else max_wait_sec
        deadline = self._clock.monotonic() + limit
        reserve = g.t.reserve if g.t.reserve is not None else self._t.reserve
        need = 1.0 + reserve.get(priority, 0.0)
        g.waiting[priority] += 1
        try:
            while True:
                now = self._clock.monotonic()
                g.bucket.rate = g.t.per_minute / 60.0 * self._rate_factor(g, now)
                g.bucket.refill(now)
                self._global.refill(now)
                blocked_until = max(self._global_paused_until, g.cooling_until)
                if now < blocked_until:
                    if blocked_until > deadline:
                        g.denied[priority] += 1
                        reason = "throttled" if self._global_paused_until > now else f"cooldown:{group}"
                        raise Denied(reason, blocked_until - now)
                    await self._clock.sleep(min(blocked_until - now, 0.5))
                    continue
                if g.probing:
                    # after a cooldown one request probes the group; the rest wait for its answer
                    if not g.probe_inflight and not self._higher_waiting(g, priority):
                        if self._start(g, priority, now, need, probe=True):
                            return
                    if now >= deadline:
                        g.denied[priority] += 1
                        raise Denied(f"cooldown:{group}", 5.0)
                    await self._clock.sleep(0.1)
                    continue
                if not self._higher_waiting(g, priority) and self._start(g, priority, now, need):
                    return
                gap, global_gap = self._gaps(g, priority, now)
                wait = max(
                    (need - g.bucket.tokens) / g.bucket.rate,
                    (1.0 - self._global.tokens) / self._global.rate,
                    g.last_start + gap - now,
                    self._global_last + global_gap - now,
                    0.02,
                )
                if now + wait > deadline:
                    g.denied[priority] += 1
                    raise Denied("budget", max(wait, 5.0))
                await self._clock.sleep(min(wait, 0.25))
        finally:
            g.waiting[priority] -= 1

    def _start(self, g: GroupState, priority: str, now: float, need: float, probe: bool = False) -> bool:
        if g.bucket.tokens < need or self._global.tokens < 1.0:
            return False
        gap, global_gap = self._gaps(g, priority, now)
        if now < g.last_start + gap or now < self._global_last + global_gap:
            return False
        g.bucket.tokens -= 1.0
        self._global.tokens -= 1.0
        g.last_start = now
        self._global_last = now
        g.starts.append(now)
        for w, n in self.windows(g).items():
            g.peak[w] = max(g.peak[w], n)
        g.granted[priority] += 1
        if probe:
            g.probe_inflight = True
        return True

    def ok(self, group: str) -> None:
        """A real answer from the group: reopen it. The ladder is NOT reset: it steps down one level per
        `cooldown_decay_sec` clean (see `_decay`), and the group runs at `reopen_rate_factor` for `reopen_slow_sec`."""
        g = self._group(group)
        if g.probing:
            g.probing = False
            g.probe_inflight = False
            g.reopened_at = self._clock.monotonic()

    def _decay(self, g: GroupState, now: float) -> None:
        """Step the ladder down one level per `cooldown_decay_sec` without a throttle (2026-10-05: /vas/ was
        re-challenged 30 s - 20 min after reopenings, so a full reset to 15 s probed straight back into blocks)."""
        if g.level <= 0 or g.cooling_until > now or g.probing:
            return
        clean_since = max(g.last_throttle, g.last_decay)
        k = int((now - clean_since) // self._t.cooldown_decay_sec)
        if k <= 0:
            return
        g.level = max(0, g.level - k)
        g.step = 0.0 if g.level == 0 else max(self._t.cooldown_start_sec, g.step / (2**k))
        g.last_decay = clean_since + k * self._t.cooldown_decay_sec

    def _gaps(self, g: GroupState, priority: str, now: float) -> tuple[float, float]:
        """(group gap, global gap) before a start of `priority`. P0 (engine token intel) may use shorter gaps, so one
        survivor's calls fit the engine's timeout; after a reopen the half rate also stretches the group gap."""
        base = g.t.min_gap_sec
        global_gap = self._t.min_gap_sec
        if priority == "P0":
            if g.t.p0_min_gap_sec is not None:
                base = g.t.p0_min_gap_sec
            global_gap = min(global_gap, self._t.p0_global_gap_sec)
        return base / self._rate_factor(g, now), global_gap

    def _rate_factor(self, g: GroupState, now: float) -> float:
        return self._t.reopen_rate_factor if now - g.reopened_at < self._t.reopen_slow_sec else 1.0

    def released(self, group: str) -> None:
        """A request finished without a verdict on throttling (network error etc.): free the probe slot."""
        g = self._group(group)
        g.probe_inflight = False

    def seed(self, group: str, level: int, step: float, remaining_sec: float) -> None:
        """Restore a cooldown ladder after a restart (from the last throttle sample): the group stays closed for the
        remaining time, then probes; a further throttle continues doubling from `step`."""
        g = self._group(group)
        now = self._clock.monotonic()
        g.level, g.step = max(0, level), max(0.0, step)
        g.last_throttle = now
        g.cooling_until = max(g.cooling_until, now + max(0.0, remaining_sec))
        g.probing = True
        g.probe_inflight = False

    def throttle(self, group: str = "other") -> float:
        """Record a 429/403 for `group`; returns that group's cooldown in seconds."""
        g = self._group(group)
        now = self._clock.monotonic()
        g.last_throttle_windows = self.windows(g)
        # escalate on every throttle (15 s … 300 s … 3600 s); the ladder only comes down by `_decay` (one level per
        # clean `cooldown_decay_sec`), so a re-challenge soon after a reopen continues where the last block ended
        self._decay(g, now)
        if g.step <= 0:
            g.step = self._t.cooldown_start_sec
        else:
            g.step = min(g.step * 2, self._t.cooldown_max_sec)
        g.level += 1
        g.cooling_until = max(g.cooling_until, now + g.step)
        g.cooldown_sec_total += g.step
        g.last_throttle = now
        g.throttles += 1
        g.probing = True
        g.probe_inflight = False
        cooling = [x for x in self._groups.values() if x.cooling_until > now]
        if len(cooling) >= self._t.global_pause_groups:
            self._global_paused_until = max(self._global_paused_until, now + self._t.global_pause_sec)
            self.global_pauses += 1
        return g.step
