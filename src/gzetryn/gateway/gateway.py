"""The only path to GMGN (spec §4, §10): cache → coalesce → budget → transport → classify → act."""

from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from gzetryn.clock import Clock
from gzetryn.config import Tunables
from gzetryn.gateway.budget import Budget, Denied
from gzetryn.gateway.cache import TtlCache, cache_key
from gzetryn.gmgn import answer as A
from gzetryn.gmgn.endpoints import Endpoint, request_class
from gzetryn.log import fields, get_logger
from gzetryn.transport.http import Transport

log = get_logger("gzetryn.gateway")


class GatewayError(Exception):
    pass


class BadRequest(GatewayError):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(GatewayError):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class UpstreamError(GatewayError):
    """GMGN answered with something we cannot use (needs login, unknown shape)."""

    def __init__(self, outcome: str, message: str):
        super().__init__(f"{outcome}: {message}")
        self.outcome = outcome
        self.message = message


class Unavailable(GatewayError):
    def __init__(self, reason: str, retry_after_sec: float):
        super().__init__(f"{reason} (retry after {retry_after_sec:.0f} s)")
        self.reason = reason
        self.retry_after_sec = retry_after_sec


@dataclass(frozen=True)
class Result:
    body: Any
    fetched_at: datetime
    cached: bool = False
    stale: bool = False
    age_sec: float = 0.0


class Hooks(Protocol):
    async def sample(self, endpoint: str, status: int, reason: str, body: str | None) -> None: ...


class NullHooks:
    async def sample(self, endpoint, status, reason, body):
        pass


class Gateway:
    def __init__(
        self,
        tunables: Tunables,
        transport: Transport,
        budget: Budget,
        cache: TtlCache | None = None,
        hooks: Hooks | None = None,
        clock: Clock | None = None,
    ):
        self._t = tunables
        self._transport = transport
        self._budget = budget
        self._clock = clock or Clock()
        self._cache = cache or TtlCache(tunables.cache.max_entries, self._clock)
        self._hooks: Hooks = hooks or NullHooks()
        self._inflight: dict[tuple, asyncio.Future] = {}
        self.counts: Counter[tuple[str, str, str]] = Counter()  # (consumer, endpoint, outcome)
        self.latency_ms: defaultdict[tuple[str, str, str], float] = defaultdict(float)
        # request classes per group (D-2026-10-05-14): P0 = engine token intel, else the endpoint name
        self.class_counts: Counter[tuple[str, str, str]] = Counter()  # (group, class, sent|ok|throttled|denied|cache)
        self._class_sent: defaultdict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=20_000))
        self.last_ok_at: datetime | None = None
        self.last_throttle_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def cache(self) -> TtlCache:
        return self._cache

    @property
    def budget(self) -> Budget:
        return self._budget

    def ttl(self, endpoint: Endpoint) -> int:
        return self._t.cache.ttl_sec.get(endpoint.name, 0)

    def class_report(self) -> dict[str, dict[str, dict]]:
        """{group: {class: {sent, ok, throttled, denied, cache, sent_last_hour}}} since start."""
        now = self._clock.monotonic()
        out: dict[str, dict[str, dict]] = {}
        keys = {(g, c) for g, c, _ in self.class_counts}
        for g, c in sorted(keys):
            sent = self._class_sent.get((g, c)) or ()
            out.setdefault(g, {})[c] = {
                **{k: self.class_counts.get((g, c, k), 0) for k in ("sent", "ok", "throttled", "denied", "cache")},
                "sent_last_hour": sum(1 for x in sent if now - x <= 3600),
            }
        return out

    def _cls(self, endpoint: Endpoint, priority: str) -> tuple[str, str]:
        return endpoint.group, request_class(endpoint, priority)

    async def call(
        self,
        endpoint: Endpoint,
        *,
        path: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        body: dict | None = None,
        priority: str = "P2",
        consumer: str = "gzetryn",
        max_age_sec: float | None = None,
    ) -> Result:
        ttl = self.ttl(endpoint)
        accept = float(ttl if max_age_sec is None else min(max_age_sec, ttl))
        key = cache_key(endpoint.name, path, params, body)
        if ttl > 0 and accept > 0:
            hit = self._cache.get(key, accept)
            if hit is not None:
                entry, age = hit
                self.counts[(consumer, endpoint.name, "cache")] += 1
                self.class_counts[(*self._cls(endpoint, priority), "cache")] += 1
                return Result(entry.value, entry.fetched_at, cached=True, age_sec=age)
        if key in self._inflight:
            self.counts[(consumer, endpoint.name, "shared")] += 1
            return await asyncio.shield(self._inflight[key])
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            result = await self._fetch(endpoint, path or {}, params or {}, body, priority, consumer, key, ttl > 0)
            fut.set_result(result)
            return result
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()  # mark retrieved: waiters re-raise it, nobody else has to
            raise
        finally:
            if self._inflight.get(key) is fut:
                del self._inflight[key]

    async def _fetch(
        self,
        endpoint: Endpoint,
        path: dict,
        params: dict,
        body: dict | None,
        priority: str,
        consumer: str,
        key: tuple,
        cacheable: bool,
    ) -> Result:
        reason, retry_after = "unavailable", 30.0
        group = endpoint.group
        gc = self._cls(endpoint, priority)
        for attempt in range(2):
            try:
                await self._budget.take(priority, group)
            except Denied as d:
                self.counts[(consumer, endpoint.name, "denied")] += 1
                self.class_counts[(*gc, "denied")] += 1
                reason, retry_after = d.reason, d.retry_after
                break
            self.class_counts[(*gc, "sent")] += 1
            self._class_sent[gc].append(self._clock.monotonic())
            raw = await self._transport.request(endpoint, path, params, body)
            v = A.classify(raw)
            ck = (consumer, endpoint.name, v.outcome)
            self.counts[ck] += 1
            self.latency_ms[ck] += raw.latency_ms
            if v.outcome in (A.OK, A.THROTTLED):
                self.class_counts[(*gc, "ok" if v.outcome == A.OK else "throttled")] += 1
            now = self._clock.now()
            if v.outcome == A.OK:
                self._budget.ok(group)
                self.last_ok_at = now
                if cacheable:
                    self._cache.put(key, raw.body, now)
                return Result(raw.body, now)
            self.last_error = f"{endpoint.name}: {v.outcome} {v.code or ''} {v.message or ''}".strip()
            if v.outcome == A.THROTTLED:
                pause = self._budget.throttle(group)
                rep = self._budget.report()[group]
                windows, level = rep["last_throttle_windows"], rep["cooldown_level"]
                self.last_throttle_at = now
                log.warning(
                    "gmgn throttled",
                    extra=fields(
                        endpoint=endpoint.name,
                        group=group,
                        status=raw.status,
                        cooldown_sec=pause,
                        cooldown_level=level,
                        windows=windows,
                    ),
                )
                head = json.dumps(
                    {"group": group, "cooldown_sec": pause, "cooldown_level": level, "requests_in_window_sec": windows}
                )
                await self._hooks.sample(endpoint.name, raw.status, v.outcome, head + " " + (raw.text_head or "")[:200])
                reason, retry_after = f"cooldown:{group}", pause
                break
            if v.outcome in A.RETRYABLE:
                self._budget.released(group)  # no verdict on throttling: free a probe slot
            else:
                self._budget.ok(group)  # GMGN itself answered: the group is not challenged
            if v.outcome == A.BAD_PARAM:
                raise BadRequest(f"GMGN refused the parameters ({v.code}: {v.message})")
            if v.outcome == A.NOT_FOUND:
                raise NotFound(f"GMGN {endpoint.name}: not found")
            if v.outcome in A.RETRYABLE:
                reason, retry_after = v.outcome, 30.0
                if attempt == 0:
                    await self._clock.sleep(self._t.transport.retry_delay_sec)
                    continue
                break
            # NEEDS_LOGIN / UNKNOWN: GMGN changed something; keep a sample
            await self._hooks.sample(endpoint.name, raw.status, v.outcome, _sample_text(raw))
            log.warning(
                "gmgn unusable answer",
                extra=fields(endpoint=endpoint.name, outcome=v.outcome, status=raw.status, code=v.code),
            )
            stale = self._stale(key, cacheable)
            if stale is not None:
                return stale
            raise UpstreamError(v.outcome, f"{endpoint.name}: HTTP {raw.status} code {v.code} {v.message or ''}")
        stale = self._stale(key, cacheable)
        if stale is not None:
            return stale
        raise Unavailable(reason, retry_after)

    def _stale(self, key: tuple, cacheable: bool) -> Result | None:
        if not cacheable:
            return None
        hit = self._cache.get_stale(key)
        if hit is None:
            return None
        entry, age = hit
        return Result(entry.value, entry.fetched_at, cached=True, stale=True, age_sec=age)


def _sample_text(raw: A.RawAnswer) -> str | None:
    if raw.body is not None:
        try:
            return json.dumps(raw.body, ensure_ascii=False, default=str)[:4000]
        except (TypeError, ValueError):
            return str(raw.body)[:4000]
    return raw.text_head or None
