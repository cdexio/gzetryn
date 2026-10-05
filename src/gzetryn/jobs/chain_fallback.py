"""Chain decoder for the feed (spec §6.2).

A swap-like notification from the WebSocket trigger is decoded from the transaction itself (`getTransaction` on
free public RPCs) and stored as the event. Since D-2026-10-05-14 (`trigger.chain_first`) this is the primary path
for every notified swap, also while GMGN's `/vas/` group is open; before, it ran only while `/vas/` was challenged.
Enrichment comes from groups that are not challenged: symbol / supply from `mutil_window_token_info` (`/api/`),
SOL/USD from GMGN's wSOL price (same endpoint), else the median of recent GMGN trades in the feed. GMGN's row for the
same (wallet, tx_hash, mint, side), found later by a sweep poll, is recognised and not stored a second time.

RPC calls are serial and paced per endpoint (the soonest free endpoint takes the next call); enrichment and the
insert run in their own tasks, so a burst of swaps is limited by the RPC pace only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

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

WSOL = "So11111111111111111111111111111111111111112"
MISSED_MAX = 5000  # signatures the decoder could not turn into an event, remembered to count GMGN finding them later


@dataclass
class Job:
    wallet: str
    signature: str
    queued: float  # monotonic
    notified_at: str
    attempt: int = 0
    not_before: float = 0.0  # monotonic; retry of a transaction the RPC had not indexed yet


@dataclass
class Rpc:
    url: str
    gap: float
    name: str
    last_call: float = -1e9
    paused_until: float = 0.0
    stats: dict = field(default_factory=lambda: {"calls": 0, "rpc_429": 0, "errors": 0, "not_found": 0})

    def free_at(self) -> float:
        return max(self.paused_until, self.last_call + self.gap)


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
        self._rpcs = [Rpc(e.url, e.min_gap_sec, urlparse(e.url).hostname or e.url) for e in t.rpc_endpoints]
        self._q: deque[Job] = deque()
        self._retry: list[Job] = []
        self._queued: set[str] = set()
        self._done: dict[str, float] = {}  # signature → monotonic time it was processed (kept 10 min, insertion order)
        self._wake = asyncio.Event()
        self._stopped = False
        self._session: AsyncSession | None = None
        self._tasks: set[asyncio.Task] = set()
        self._sol_usd: tuple[float, float | None, str | None] = (-1e9, None, None)  # (monotonic, value, source)
        # the watcher sets this: called with (wallet, signature, reason) when no event came out of a notification
        self.on_miss: Callable[[str, str, str], None] | None = None
        self.missed: OrderedDict[str, str] = OrderedDict()  # signature → reason
        self.decode_delay = deque(maxlen=2000)
        self.stats = {
            "queued": 0,
            "dropped_old": 0,
            "dropped_full": 0,
            "rpc_calls": 0,
            "rpc_429": 0,
            "rpc_errors": 0,
            "not_found": 0,
            "fetch_failed": 0,
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

    @property
    def backlog(self) -> int:
        return len(self._q) + len(self._retry)

    def enqueue(self, wallet: str, signature: str, notified_at: str) -> None:
        mono = self._clock.monotonic()
        while self._done and mono - self._done[next(iter(self._done))] > 600:
            self._done.pop(next(iter(self._done)))
        if signature in self._queued or signature in self._done:
            self.stats["skipped_known"] += 1  # already queued or decoded (failed trigger retries re-route it)
            return
        if self.backlog >= self._t.chain_queue_max:
            self.stats["dropped_full"] += 1
            self._miss(wallet, signature, "dropped_full")
            return
        self._q.append(Job(wallet, signature, mono, notified_at))
        self._queued.add(signature)
        self.stats["queued"] += 1
        self._wake.set()

    def _miss(self, wallet: str, signature: str, reason: str) -> None:
        self.missed[signature] = reason
        self.missed.move_to_end(signature)
        while len(self.missed) > MISSED_MAX:
            self.missed.popitem(last=False)
        if self.on_miss is not None:
            try:
                self.on_miss(wallet, signature, reason)
            except Exception:
                log.exception("chain miss callback failed")

    def _next_job(self) -> Job | None:
        now = self._clock.monotonic()
        for i, j in enumerate(self._retry):
            if j.not_before <= now:
                return self._retry.pop(i)
        return self._q.popleft() if self._q else None

    async def run(self) -> None:
        self._session = AsyncSession(impersonate="chrome", timeout=15)
        try:
            while not self._stopped:
                job = self._next_job()
                if job is None:
                    self._wake.clear()
                    wait = 5.0
                    if self._retry:
                        wait = max(0.05, min(j.not_before for j in self._retry) - self._clock.monotonic())
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=wait)
                    continue
                if self._clock.monotonic() - job.queued > self._t.chain_max_age_sec:
                    self._finish(job)
                    self.stats["dropped_old"] += 1
                    self._miss(job.wallet, job.signature, "dropped_old")
                    continue
                try:
                    await self._fetch(job)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # keep the worker alive
                    self._finish(job)
                    self.stats["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                    log.exception("chain fetch failed", extra=fields(sig=job.signature))
        finally:
            for t in list(self._tasks):
                t.cancel()
            with contextlib.suppress(Exception):
                await self._session.close()

    def _finish(self, job: Job) -> None:
        self._queued.discard(job.signature)
        self._done[job.signature] = self._clock.monotonic()

    async def _pace(self) -> Rpc:
        rpc = min(self._rpcs, key=lambda r: r.free_at())
        wait = rpc.free_at() - self._clock.monotonic()
        if wait > 0:
            await self._clock.sleep(wait * random.uniform(1.0, 1.1))
        rpc.last_call = self._clock.monotonic()
        return rpc

    async def _fetch(self, job: Job) -> None:
        """One getTransaction for the job; not indexed yet → retried later without blocking the queue."""
        if self._context(job.wallet) is None:
            self._finish(job)
            return
        rpc = await self._pace()
        state, result = await self._get_tx(rpc, job.signature)
        if state == "retry" and job.attempt + 1 < self._t.rpc_retries:
            job.attempt += 1
            job.not_before = self._clock.monotonic() + 2.0
            self._retry.append(job)
            return
        self._finish(job)
        if result is None:
            self.stats["fetch_failed"] += 1
            self._miss(job.wallet, job.signature, "fetch_failed")
            return
        task = asyncio.create_task(self._store(job, result, rpc.name), name=f"chain:{job.signature[:6]}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _get_tx(self, rpc: Rpc, signature: str) -> tuple[str, dict | None]:
        """('ok', result) | ('retry', None) for not indexed / 429 / network | ('error', None)."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}],
        }
        self.stats["rpc_calls"] += 1
        rpc.stats["calls"] += 1
        try:
            r = await self._session.post(rpc.url, data=json.dumps(body), headers={"Content-Type": "application/json"})
        except Exception as e:  # network
            self.stats["rpc_errors"] += 1
            rpc.stats["errors"] += 1
            self.stats["last_error"] = f"rpc {rpc.name}: {type(e).__name__}"
            return "retry", None
        if r.status_code == 429:
            self.stats["rpc_429"] += 1
            rpc.stats["rpc_429"] += 1
            rpc.paused_until = self._clock.monotonic() + self._t.rpc_backoff_sec
            return "retry", None
        try:
            res = r.json()
        except ValueError:
            self.stats["rpc_errors"] += 1
            rpc.stats["errors"] += 1
            self.stats["last_error"] = f"rpc {rpc.name}: HTTP {r.status_code}, not JSON"
            return "retry", None
        if res.get("result"):
            return "ok", res["result"]
        if res.get("error"):
            self.stats["rpc_errors"] += 1
            rpc.stats["errors"] += 1
            self.stats["last_error"] = f"rpc {rpc.name}: {str(res['error'])[:150]}"
            return "error", None
        self.stats["not_found"] += 1  # not indexed by the RPC yet
        rpc.stats["not_found"] += 1
        return "retry", None

    async def sol_usd(self) -> tuple[float | None, str | None]:
        """SOL/USD: GMGN's wSOL price (/api/, P1), else the median of recent GMGN trades in the feed."""
        mono = self._clock.monotonic()
        at, value, src = self._sol_usd
        if mono - at < 60 and value is not None:
            return value, src
        value, src = None, None
        with contextlib.suppress(GatewayError):
            r = await self._gw.call(E.TOKEN_WINDOW_INFO, body={"chain": E.CHAIN, "addresses": [WSOL]}, priority="P1")
            p = parse.token_window_info(r.body)
            if p is not None and p["info"]["mint"] == WSOL and p["price"].get("price_usd"):
                value, src = float(p["price"]["price_usd"]), "gmgn_wsol"
        if value is None:
            # a long /vas/ challenge leaves no recent GMGN trades: widen to 24 h rather than drop the USD fields
            value = await self._feed.recent_sol_usd(self._t.sol_usd_window_min) or await self._feed.recent_sol_usd(1440)
            src = "feed_median" if value is not None else None
        self._sol_usd = (mono, value, src)
        return value, src

    async def _store(self, job: Job, result: dict, rpc_name: str) -> None:
        try:
            await self._process(job, result, rpc_name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.stats["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
            log.exception("chain event failed", extra=fields(sig=job.signature))

    async def _process(self, job: Job, result: dict, rpc_name: str) -> None:
        ctx = self._context(job.wallet)
        if ctx is None:
            return
        trades = decode(result, job.wallet, job.signature)
        if not trades:
            self.stats["not_a_swap"] += 1
            self._miss(job.wallet, job.signature, "not_a_swap")
            return
        self.stats["decoded"] += 1
        ct = trades[0]
        symbol = supply = None
        with contextlib.suppress(GatewayError):
            # P1: below engine token intel (P0), above the background lists (D-2026-10-05-14)
            r = await self._gw.call(
                E.TOKEN_WINDOW_INFO, body={"chain": E.CHAIN, "addresses": [ct.mint]}, priority="P1", consumer="gzetryn"
            )
            p = parse.token_window_info(r.body)
            if p is not None and p["info"]["mint"] == ct.mint:
                symbol, supply = p["info"]["symbol"], p["info"]["total_supply"]
        sol_usd, sol_usd_src = await self.sol_usd()
        price_sol = ct.sol_amount / ct.token_amount if ct.token_amount else None
        price_usd = price_sol * sol_usd if price_sol is not None and sol_usd else None
        delay = round(self._clock.monotonic() - job.queued, 2)
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
                "sol_usd_source": sol_usd_src,
                "sol_includes_fees": True,
                "decode_delay_sec": delay,
                "rpc": rpc_name,
            },
        )
        new = await self._feed.insert(ctx, [row], self._clock.now())
        if new:
            self.stats["events"] += 1
            self.stats["last_event_at"] = self._clock.now().isoformat()
            self.decode_delay.append(delay)
            log.info("chain event", extra=fields(wallet=job.wallet, tx=job.signature, side=ct.side, symbol=symbol))
        else:
            self.stats["duplicates"] += 1  # GMGN already delivered it

    def summary(self) -> dict:
        now = self._clock.monotonic()
        d = sorted(self.decode_delay)
        pct = (lambda p: round(d[min(len(d) - 1, int(p * len(d)))], 2) if d else None)
        return {
            **self.stats,
            "queue": len(self._q),
            "retrying": len(self._retry),
            "decode_delay_sec_p50": pct(0.5),
            "decode_delay_sec_p90": pct(0.9),
            "rpc": {
                r.name: {**r.stats, "min_gap_sec": r.gap, "paused_sec": round(max(0.0, r.paused_until - now), 1)}
                for r in self._rpcs
            },
        }
