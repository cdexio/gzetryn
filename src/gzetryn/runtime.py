"""Wires config, store, transport, budget, gateway and jobs into one process (spec §4, §13)."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from datetime import datetime

from gzetryn.clock import Clock
from gzetryn.config import Settings, Tunables
from gzetryn.gateway.budget import Budget
from gzetryn.gateway.gateway import Gateway
from gzetryn.jobs.directory import Directory
from gzetryn.jobs.token import TokenIntel
from gzetryn.jobs.watcher import Watcher
from gzetryn.log import fields, get_logger
from gzetryn.store.db import make_engine, make_sessionmaker
from gzetryn.store.feed import FeedStore
from gzetryn.store.ops import OpsStore
from gzetryn.store.wallets import WalletStore
from gzetryn.transport.http import HttpTransport
from gzetryn.trigger.solana_ws import WsTrigger

log = get_logger("gzetryn.runtime")


class SampleHooks:
    def __init__(self, ops: OpsStore, clock: Clock):
        self._ops = ops
        self._clock = clock

    async def sample(self, endpoint: str, status: int, reason: str, body: str | None) -> None:
        try:
            await self._ops.add_sample(self._clock.now(), endpoint, status, reason, body)
        except Exception:
            log.exception("sample write failed")


class Runtime:
    def __init__(self, settings: Settings, tunables: Tunables | None = None, clock: Clock | None = None, transport=None):
        self.settings = settings
        self.t = tunables or settings.tunables
        self.clock = clock or Clock()
        self.engine = make_engine(settings.database_url, schema=settings.db_schema)
        self.sessions = make_sessionmaker(self.engine)
        self.wallets = WalletStore(self.sessions)
        self.feed = FeedStore(self.sessions)
        self.ops = OpsStore(self.sessions)
        self.transport = transport or HttpTransport(self.t.transport.impersonate, self.t.transport.timeout_sec)
        self.budget = Budget(self.t.budget, self.clock)
        self.gateway = Gateway(self.t, self.transport, self.budget, hooks=SampleHooks(self.ops, self.clock), clock=self.clock)
        self.directory = Directory(self.t, self.gateway, self.wallets, self.clock)
        self.watcher = Watcher(self.t.watch, self.gateway, self.wallets, self.feed, self.clock, self.t.trigger)
        self.trigger: WsTrigger | None = None
        if self.t.trigger.enabled:
            self.trigger = WsTrigger(self.t.trigger, self.watcher.on_trade, self.clock)
            self.watcher.trigger = self.trigger
        self.token = TokenIntel(self.t.token, self.gateway, self.wallets, self.feed)
        self._flushed: Counter = Counter()
        self._flushed_lat: defaultdict = defaultdict(float)
        self._tasks: list[asyncio.Task] = []
        self.started_at: datetime | None = None

    async def start(self, background: bool = True) -> None:
        self.started_at = self.clock.now()
        if not background:
            return
        if self.t.rank.enabled:
            self._tasks.append(asyncio.create_task(self.directory.run(), name="directory"))
        if self.t.watch.enabled:
            self._tasks.append(asyncio.create_task(self.watcher.run(), name="watcher"))
            if self.trigger is not None:
                self._tasks.append(asyncio.create_task(self.trigger.run(), name="trigger"))
        self._tasks.append(asyncio.create_task(self._every(60, self._flush_counts), name="counts"))
        self._tasks.append(
            asyncio.create_task(self._every(self.t.retention.interval_sec, self._retention, 900), name="retention")
        )
        log.info("runtime started", extra=fields(port=self.settings.port, rpm=self.t.budget.per_minute))

    async def _every(self, seconds: float, fn, first_delay: float | None = None) -> None:
        await self.clock.sleep(seconds if first_delay is None else first_delay)
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("background job failed", extra=fields(job=getattr(fn, "__name__", "?")))
            await self.clock.sleep(seconds)

    async def _flush_counts(self) -> None:
        current = Counter(self.gateway.counts)
        delta = current - self._flushed
        lat = {k: v - self._flushed_lat.get(k, 0.0) for k, v in self.gateway.latency_ms.items()}
        await self.ops.flush_counts(delta, lat, self.clock.now())
        self._flushed = current
        self._flushed_lat = defaultdict(float, self.gateway.latency_ms)

    async def _retention(self) -> None:
        removed = await self.ops.retention(self.t.retention, self.clock.now())
        log.info("retention", extra=fields(**removed))

    async def stop(self) -> None:
        self.directory.stop()
        self.watcher.stop()
        if self.trigger is not None:
            self.trigger.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await self._flush_counts()
        except Exception:
            log.exception("final count flush failed")
        await self.transport.close()
        await self.engine.dispose()
