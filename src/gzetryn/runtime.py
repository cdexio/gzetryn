"""Wires config, store, transport, budget, gateway and jobs into one process (spec §4, §13)."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from gzetryn.clock import Clock
from gzetryn.config import Settings, Tunables
from gzetryn.gateway.budget import Budget
from gzetryn.gateway.gateway import Gateway
from gzetryn.gmgn import endpoints as E
from gzetryn.jobs.candidates import Candidates
from gzetryn.jobs.chain_fallback import ChainFallback
from gzetryn.jobs.dextools import DexTools
from gzetryn.jobs.directory import Directory
from gzetryn.jobs.launchlab import LaunchLab
from gzetryn.jobs.pump_chain import PumpChain
from gzetryn.trigger.solana import PUMP_PROGRAM
from gzetryn.jobs.token import TokenIntel
from gzetryn.jobs.watcher import Watcher
from gzetryn.log import fields, get_logger
from gzetryn.store.candidates import CandidateStore
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
        self.chain: ChainFallback | None = None
        if self.t.trigger.enabled:
            self.trigger = WsTrigger(self.t.trigger, self.watcher.on_trade, self.clock)
            self.watcher.trigger = self.trigger
            if self.t.trigger.chain_fallback:
                self.chain = ChainFallback(self.t.trigger, self.gateway, self.feed, self.watcher.context_of, self.clock)
                self.chain.on_miss = self.watcher.on_chain_miss
                self.watcher.chain = self.chain
                self.watcher.gmgn_open = lambda: self.budget.is_open(E.WALLET_ACTIVITY.group)
        self.dextools: DexTools | None = DexTools(self.t.dextools, self.budget, self.clock) if self.t.dextools.enabled else None
        self.token = TokenIntel(self.t.token, self.gateway, self.wallets, self.feed, self.dextools)
        self.candidate_store = CandidateStore(self.sessions)
        self.pump_chain: PumpChain | None = None
        if self.trigger is not None and self.t.pump_chain.enabled:
            async def sol_usd():
                if self.chain is not None:  # GMGN's wSOL price, else the feed median (fewer GMGN trades since chain-first)
                    return (await self.chain.sol_usd())[0]
                return await self.feed.recent_sol_usd(30) or await self.feed.recent_sol_usd(1440)

            self.pump_chain = PumpChain(self.t.pump_chain, self.candidate_store, sol_usd, self.clock)
            self.trigger.add_program(PUMP_PROGRAM, self.pump_chain.on_logs)
        self.candidates: Candidates | None = (
            Candidates(self.t.candidates, self.gateway, self.candidate_store, self.clock)
            if self.t.candidates.enabled
            else None
        )
        if self.candidates is not None and self.pump_chain is not None:
            pc, idle = self.pump_chain, self.t.candidates.pump_chain_max_idle_sec
            self.candidates.pump_chain_healthy = lambda: pc.healthy(idle)
        self.launchlab: LaunchLab | None = (
            LaunchLab(self.t.launchlab, self.budget, self.candidate_store, self.clock) if self.t.launchlab.enabled else None
        )
        self._flushed: Counter = Counter()
        self._flushed_lat: defaultdict = defaultdict(float)
        self._tasks: list[asyncio.Task] = []
        self.started_at: datetime | None = None

    async def restore_cooldowns(self) -> dict[str, dict]:
        """Re-open the cooldown ladders that were running before a restart (spec §10), so a deploy during a GMGN
        block does not start probing again at 15 s."""
        now = self.clock.now()
        restored = {}
        try:
            last = await self.ops.last_throttles(now - timedelta(seconds=self.t.budget.cooldown_max_sec))
        except Exception:
            log.exception("cooldown restore failed")
            return {}
        for group, d in last.items():
            remaining = d["cooldown_sec"] - (now - d["at"]).total_seconds()
            self.budget.seed(group, d["cooldown_level"], d["cooldown_sec"], remaining)
            restored[group] = {"level": d["cooldown_level"], "step_sec": d["cooldown_sec"], "remaining_sec": round(max(0, remaining))}
        if restored:
            log.info("cooldowns restored", extra=fields(groups=restored))
        return restored

    async def start(self, background: bool = True) -> None:
        self.started_at = self.clock.now()
        if not background:
            return
        await self.restore_cooldowns()
        if self.t.rank.enabled:
            self._tasks.append(asyncio.create_task(self.directory.run(), name="directory"))
        if self.t.watch.enabled:
            self._tasks.append(asyncio.create_task(self.watcher.run(), name="watcher"))
            if self.trigger is not None:
                self._tasks.append(asyncio.create_task(self.trigger.run(), name="trigger"))
            if self.chain is not None:
                self._tasks.append(asyncio.create_task(self.chain.run(), name="chain"))
        if self.candidates is not None:
            self._tasks.append(asyncio.create_task(self.candidates.run(), name="candidates"))
        if self.pump_chain is not None and self.t.watch.enabled:
            self._tasks.append(asyncio.create_task(self.pump_chain.run(), name="pump_chain"))
        if self.launchlab is not None:
            self._tasks.append(asyncio.create_task(self.launchlab.run(), name="launchlab"))
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
        removed["candidates"] = await self.candidate_store.retention(self.t.candidates.retention_days, self.clock.now())
        log.info("retention", extra=fields(**removed))

    async def stop(self) -> None:
        self.directory.stop()
        self.watcher.stop()
        if self.trigger is not None:
            self.trigger.stop()
        if self.chain is not None:
            self.chain.stop()
        if self.candidates is not None:
            self.candidates.stop()
        if self.launchlab is not None:
            self.launchlab.stop()
        if self.pump_chain is not None:
            self.pump_chain.stop()
            try:
                await self.pump_chain.flush()
            except Exception:
                log.exception("final pump chain flush failed")
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await self._flush_counts()
        except Exception:
            log.exception("final count flush failed")
        await self.transport.close()
        if self.dextools is not None:
            await self.dextools.close()
        await self.engine.dispose()
