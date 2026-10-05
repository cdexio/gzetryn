"""Chain fallback for the feed (spec §6.2).

While GMGN's `/vas/` group (wallet_activity) is challenged, a swap-like notification from the WebSocket trigger is
decoded from the transaction itself (`getTransaction` on the free public RPC) and stored as the event, so the feed
stays real-time. Enrichment comes from groups that are not challenged: symbol / supply from `mutil_window_token_info`
(`/api/`), SOL/USD from the median of recent GMGN trades in the feed. When `/vas/` reopens, GMGN's row for the same
(wallet, tx_hash, mint, side) is recognised and not stored a second time.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from curl_cffi.requests import AsyncSession

from gzetryn.clock import Clock
from gzetryn.config import TriggerTunables
from gzetryn.gateway.gateway import Gateway, GatewayError
from gzetryn.gmgn import endpoints as E
from gzetryn.gmgn import parse
from gzetryn.gmgn.parse import TradeRow
from gzetryn.log import fields, get_logger
from gzetryn.store.feed import FeedStore, WalletContext
from gzetryn.trigger.chain import decode

log = get_logger("gzetryn.chain")


@dataclass
class Job:
    wallet: str
    signature: str
    queued: float  # monotonic
    notified_at: str


class ChainFallback:
    def __init__(
        self,
        t: TriggerTunables,
        gateway: Gateway,
        feed: FeedStore,
        context: Callable[[str], WalletContext | None],
        clock: Clock | None = None,
    ):
        self._t = t
        self._gw = gateway
        self._feed = feed
        self._context = context
        self._clock = clock or Clock()
        self._q: deque[Job] = deque()
        self._queued: set[str] = set()
        self._done: dict[str, float] = {}  # signature → monotonic time it was processed (kept 10 min, insertion order)
        self._wake = asyncio.Event()
        self._stopped = False
        self._session: AsyncSession | None = None
        self._last_call = -1e9
        self._paused_until = 0.0
        self._sol_usd: tuple[float, float | None] = (-1e9, None)  # (monotonic, value)
        self.stats = {
            "queued": 0,
            "dropped_old": 0,
            "dropped_full": 0,
            "rpc_calls": 0,
            "rpc_429": 0,
            "rpc_errors": 0,
            "not_found": 0,
            "decoded": 0,
            "not_a_swap": 0,
            "events": 0,
            "duplicates": 0,
            "skipped_known": 0,
            "last_event_at": None,
            "last_error": None,
        }

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    def enqueue(self, wallet: str, signature: str, notified_at: str) -> None:
        mono = self._clock.monotonic()
        while self._done and mono - self._done[next(iter(self._done))] > 600:
            self._done.pop(next(iter(self._done)))
        if signature in self._queued or signature in self._done:
            self.stats["skipped_known"] += 1  # already queued or decoded (failed trigger retries re-route it)
            return
        if len(self._q) >= self._t.chain_queue_max:
            self.stats["dropped_full"] += 1
            return
        self._q.append(Job(wallet, signature, self._clock.monotonic(), notified_at))
        self._queued.add(signature)
        self.stats["queued"] += 1
        self._wake.set()

    async def run(self) -> None:
        self._session = AsyncSession(impersonate="chrome", timeout=15)
        try:
            while not self._stopped:
                if not self._q:
                    self._wake.clear()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=5.0)
                    continue
                job = self._q.popleft()
                self._queued.discard(job.signature)
                self._done[job.signature] = self._clock.monotonic()
                if self._clock.monotonic() - job.queued > self._t.chain_max_age_sec:
                    self.stats["dropped_old"] += 1
                    continue
                try:
                    await self._process(job)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # keep the worker alive
                    self.stats["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                    log.exception("chain fallback failed", extra=fields(sig=job.signature))
        finally:
            with contextlib.suppress(Exception):
                await self._session.close()

    async def _pace(self) -> None:
        now = self._clock.monotonic()
        wait = max(self._paused_until - now, self._last_call + self._t.rpc_min_gap_sec - now, 0.0)
        if wait > 0:
            await self._clock.sleep(wait * random.uniform(1.0, 1.1))
        self._last_call = self._clock.monotonic()

    async def _get_tx(self, signature: str) -> dict | None:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}],
        }
        for _ in range(self._t.rpc_retries):
            await self._pace()
            self.stats["rpc_calls"] += 1
            try:
                r = await self._session.post(
                    self._t.rpc_url, data=json.dumps(body), headers={"Content-Type": "application/json"}
                )
            except Exception as e:  # network
                self.stats["rpc_errors"] += 1
                self.stats["last_error"] = f"rpc: {type(e).__name__}"
                continue
            if r.status_code == 429:
                self.stats["rpc_429"] += 1
                self._paused_until = self._clock.monotonic() + self._t.rpc_backoff_sec
                continue
            try:
                res = r.json()
            except ValueError:
                self.stats["rpc_errors"] += 1
                continue
            if res.get("result"):
                return res["result"]
            if res.get("error"):
                self.stats["rpc_errors"] += 1
                self.stats["last_error"] = f"rpc: {str(res['error'])[:150]}"
                return None
            self.stats["not_found"] += 1  # not indexed by the RPC yet
            await self._clock.sleep(2.0)
        return None

    async def sol_usd(self) -> float | None:
        mono = self._clock.monotonic()
        at, value = self._sol_usd
        if mono - at < 60 and value is not None:
            return value
        # a long /vas/ challenge leaves no recent GMGN trades: widen to 24 h rather than drop the USD fields
        value = await self._feed.recent_sol_usd(self._t.sol_usd_window_min) or await self._feed.recent_sol_usd(1440)
        self._sol_usd = (mono, value)
        return value

    async def _process(self, job: Job) -> None:
        ctx = self._context(job.wallet)
        if ctx is None:
            return
        result = await self._get_tx(job.signature)
        if result is None:
            return
        trades = decode(result, job.wallet, job.signature)
        if not trades:
            self.stats["not_a_swap"] += 1
            return
        self.stats["decoded"] += 1
        ct = trades[0]
        symbol = supply = None
        with contextlib.suppress(GatewayError):
            r = await self._gw.call(
                E.TOKEN_WINDOW_INFO, body={"chain": E.CHAIN, "addresses": [ct.mint]}, priority="P0", consumer="gzetryn"
            )
            p = parse.token_window_info(r.body)
            if p is not None and p["info"]["mint"] == ct.mint:
                symbol, supply = p["info"]["symbol"], p["info"]["total_supply"]
        sol_usd = await self.sol_usd()
        price_sol = ct.sol_amount / ct.token_amount if ct.token_amount else None
        price_usd = price_sol * sol_usd if price_sol is not None and sol_usd else None
        row = TradeRow(
            wallet=job.wallet,
            tx_hash=job.signature,
            trade_at=ct.trade_at or self._clock.now(),
            side=ct.side,
            mint=ct.mint,
            symbol=symbol,
            token_amount=ct.token_amount,
            sol_amount=ct.sol_amount,
            quote_symbol="SOL",
            usd_amount=ct.sol_amount * sol_usd if sol_usd else None,
            price_usd=price_usd,
            price_sol=price_sol,
            total_supply=supply,
            mcap_usd=price_usd * supply if price_usd is not None and supply else None,
            open_or_close=None,
            launchpad=None,
            launchpad_platform=None,
            payload={
                "source": "chain",
                "notified": "swap",
                "notified_at": job.notified_at,
                "sol_usd": sol_usd,
                "sol_includes_fees": True,
                "decode_delay_sec": round(self._clock.monotonic() - job.queued, 2),
            },
        )
        new = await self._feed.insert(ctx, [row], self._clock.now())
        if new:
            self.stats["events"] += 1
            self.stats["last_event_at"] = self._clock.now().isoformat()
            log.info("chain event", extra=fields(wallet=job.wallet, tx=job.signature, side=ct.side, symbol=symbol))
        else:
            self.stats["duplicates"] += 1  # GMGN already delivered it

    def summary(self) -> dict:
        paused = round(max(0.0, self._paused_until - self._clock.monotonic()), 1)
        return {**self.stats, "queue": len(self._q), "rpc_paused_sec": paused}
