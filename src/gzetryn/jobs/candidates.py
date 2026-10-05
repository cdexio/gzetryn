"""Candidate source job (spec §7.1): pump.fun lists (P2, skipped while the pump.fun chain source is healthy), new
pairs and trending (P3) polled in the background into the `candidates` table; the engine reads it with a cursor
(`/v1/market/candidates`) at no GMGN cost.

Trending rows carry no pool address: new trending mints get it from `mutil_window_token_info` (batches of 5) before
they are stored, so every candidate has a pool when the engine first reads it (unless GMGN has none).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from gzetryn.clock import Clock
from gzetryn.config import CandidatesTunables
from gzetryn.gateway.gateway import Gateway, GatewayError
from gzetryn.gmgn import endpoints as E
from gzetryn.gmgn import parse
from gzetryn.log import fields, get_logger
from gzetryn.store.candidates import CandidateStore

log = get_logger("gzetryn.candidates")


class Candidates:
    def __init__(self, t: CandidatesTunables, gateway: Gateway, store: CandidateStore, clock: Clock | None = None):
        self._t = t
        self._gw = gateway
        self._store = store
        self._clock = clock or Clock()
        self._stopped = False
        self._next: dict[str, float] = {"pump": 0.0, "new_pairs": 3.0, "trending": 6.0}
        self.stats: dict[str, dict] = {k: {"ok": 0, "failed": 0, "new": 0, "last_ok_at": None, "last_error": None}
                                       for k in self._next}
        self.stats["pump"]["skipped_chain_healthy"] = 0
        # set by the runtime: is the pump.fun chain source (spec §7.2) delivering? Then the /vas/ pump lists are
        # skipped (D-2026-10-05-14)
        self.pump_chain_healthy: Callable[[], bool] = lambda: False
        self.pools_resolved = 0
        self.pools_missing = 0

    def stop(self) -> None:
        self._stopped = True

    async def pump(self) -> int | None:
        if self._t.pump_skip_when_chain_healthy and self.pump_chain_healthy():
            self.stats["pump"]["skipped_chain_healthy"] += 1
            return None
        # P2: in /vas/ below engine token intel (P0), above the wallet_activity sweep (P3)
        r = await self._gw.call(
            E.PUMP_LISTS, body=E.pump_lists_body(self._t.pump_limit), priority="P2", max_age_sec=self._t.pump_sec / 2
        )
        return await self._store.upsert(parse.candidates_pump(r.body), self._clock.now())

    async def new_pairs(self) -> int:
        r = await self._gw.call(
            E.NEW_PAIRS,
            path={"interval": self._t.new_pairs_interval},
            params={"limit": self._t.new_pairs_limit},
            priority="P3",
            max_age_sec=self._t.new_pairs_sec / 2,
        )
        return await self._store.upsert(parse.candidates_new_pairs(r.body), self._clock.now())

    async def trending(self) -> int:
        r = await self._gw.call(
            E.RANK_SWAPS,
            path={"interval": self._t.trending_interval},
            params={"limit": self._t.trending_limit},
            priority="P3",
            max_age_sec=self._t.trending_sec / 2,
        )
        rows = parse.candidates_trending(r.body)
        known = await self._store.known("trending", [x.mint for x in rows])
        todo = [x for x in rows if x.mint not in known][: self._t.pool_max_per_cycle]
        for i in range(0, len(todo), self._t.pool_batch):
            batch = todo[i : i + self._t.pool_batch]
            try:
                res = await self._gw.call(
                    E.TOKEN_WINDOW_INFO,
                    body={"chain": E.CHAIN, "addresses": [x.mint for x in batch]},
                    priority="P3",
                )
                pools = parse.pools_from_window_info(res.body)
            except GatewayError as e:
                log.warning("pool resolution failed", extra=fields(error=str(e)))
                pools = {}
            for x in batch:
                p = pools.get(x.mint) or {}
                x.pool_address = p.get("pool_address")
                if x.pool_address:
                    self.pools_resolved += 1
                else:
                    self.pools_missing += 1
        # mints beyond pool_max_per_cycle wait for the next cycle (they are not stored without a pool attempt)
        later = {x.mint for x in rows if x.mint not in known} - {x.mint for x in todo}
        return await self._store.upsert([x for x in rows if x.mint not in later], self._clock.now())

    async def run(self) -> None:
        jobs = {"pump": (self.pump, self._t.pump_sec), "new_pairs": (self.new_pairs, self._t.new_pairs_sec),
                "trending": (self.trending, self._t.trending_sec)}
        start = self._clock.monotonic()
        self._next = {k: start + v for k, v in self._next.items()}
        while not self._stopped:
            now = self._clock.monotonic()
            for name, (fn, every) in jobs.items():
                if now < self._next[name]:
                    continue
                self._next[name] = now + every
                st = self.stats[name]
                try:
                    n = await fn()
                    if n is None:  # skipped (pump lists while the chain source is healthy)
                        continue
                    st["ok"] += 1
                    st["new"] += n
                    st["last_ok_at"] = self._clock.now().isoformat()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # GatewayError or parse problems: keep the loop alive
                    st["failed"] += 1
                    st["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
            with contextlib.suppress(Exception):
                await self._clock.sleep(1.0)

    def summary(self) -> dict:
        return {"jobs": self.stats, "pools_resolved": self.pools_resolved, "pools_missing": self.pools_missing}
