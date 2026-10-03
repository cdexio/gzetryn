"""On-chain trigger (spec §6.1): Solana `logsSubscribe` (mentions = wallet) over a free WebSocket.

One connection, one subscription per active wallet, kept in sync with the watcher's active set. A successful
(`err` null) notification whose logs look like a swap calls `on_trade(wallet, signature, slot)`; the watcher then
polls GMGN for that wallet. The trigger never writes events itself: GMGN stays the source of the enriched event.

Verified 2026-10-03 on wss://api.mainnet-beta.solana.com from the VPS (phase 7 report): 43/43 subscriptions on one
connection, 0 disconnects in 10 min, notification 1.1-1.7 s after the (whole-second) block time, every GMGN trade
in the window notified; 99% of notifications are failed bot transactions (dropped here).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import websockets

from gzetryn.clock import Clock
from gzetryn.config import TriggerTunables
from gzetryn.log import fields, get_logger

log = get_logger("gzetryn.trigger")

_INSTR = re.compile(r"Instruction: (\w+)")
_SWAP_WORDS = ("buy", "sell", "swap", "route", "exactin", "exactout", "exact_in", "exact_out")


def swap_like(logs: list[str] | None) -> bool:
    """True when the logs show a token transfer plus a swap-style instruction (or a Raydium ray_log).

    From phase 7 samples: GMGN trades log `TransferChecked` + `BuyExactQuoteIn`/`Swap2`/`Route`; non-trades log
    account setup, plain SOL transfers, fee distribution, or bot programs without token transfers.
    """
    if not logs:
        return False
    instrs = [m.lower() for line in logs for m in _INSTR.findall(line)]
    transfer = any(i in ("transfer", "transferchecked") for i in instrs)
    if not transfer:
        return False
    if any(w in i for i in instrs for w in _SWAP_WORDS):
        return True
    return any("ray_log" in line for line in logs)


@dataclass
class TriggerStats:
    connects: int = 0
    disconnects: int = 0
    last_disconnect: str | None = None
    connected_since: datetime | None = None
    notifications: int = 0
    failed_tx: int = 0
    ok_tx: int = 0
    swap_like: int = 0
    skipped: int = 0
    bytes: int = 0
    last_notification_at: datetime | None = None
    sub_errors: int = 0


class SigCache:
    """Recently notified signatures: sig → (received_at monotonic, swap_like). Bounded and time-limited."""

    def __init__(self, ttl_sec: float, max_entries: int = 50000):
        self._ttl = ttl_sec
        self._max = max_entries
        self._d: OrderedDict[str, tuple[float, bool, datetime]] = OrderedDict()

    def put(self, sig: str, mono: float, swap: bool, at: datetime) -> None:
        self._d[sig] = (mono, swap, at)
        self._d.move_to_end(sig)
        while len(self._d) > self._max:
            self._d.popitem(last=False)

    def get(self, sig: str, mono: float) -> tuple[float, bool, datetime] | None:
        v = self._d.get(sig)
        if v is None or mono - v[0] > self._ttl:
            return None
        return v

    def prune(self, mono: float) -> None:
        while self._d:
            k, v = next(iter(self._d.items()))
            if mono - v[0] <= self._ttl:
                break
            self._d.popitem(last=False)


class WsTrigger:
    def __init__(self, t: TriggerTunables, on_trade: Callable[[str, str, int], None], clock: Clock | None = None):
        self._t = t
        self._on_trade = on_trade
        self._clock = clock or Clock()
        self._desired: set[str] = set()
        self._subs: dict[int, str] = {}  # subscription id → wallet
        self._by_wallet: dict[str, int] = {}
        self._pending: dict[int, tuple[str, str]] = {}  # request id → (kind, wallet)
        self._next_id = 1
        self._changed = asyncio.Event()
        self._stopped = False
        self._connected = False
        self.stats = TriggerStats()
        self.sigs = SigCache(t.sig_cache_sec)

    # ---------- public ----------

    def set_wallets(self, wallets: set[str]) -> None:
        if wallets != self._desired:
            self._desired = set(wallets)
            self._changed.set()

    @property
    def healthy(self) -> bool:
        """Connected and every active wallet subscribed: interval polling may run at the slow fallback pace."""
        return self._connected and bool(self._desired) and self._desired <= set(self._by_wallet)

    def stop(self) -> None:
        self._stopped = True
        self._changed.set()

    def summary(self) -> dict:
        s = self.stats
        return {
            "url": self._t.ws_url,
            "commitment": self._t.commitment,
            "connected": self._connected,
            "healthy": self.healthy,
            "connected_since": s.connected_since.isoformat() if s.connected_since else None,
            "subscriptions": len(self._by_wallet),
            "wallets": len(self._desired),
            "connects": s.connects,
            "reconnects": max(0, s.connects - 1),
            "disconnects": s.disconnects,
            "last_disconnect": s.last_disconnect,
            "notifications": s.notifications,
            "failed_tx": s.failed_tx,
            "ok_tx": s.ok_tx,
            "swap_like": s.swap_like,
            "skipped_not_swap": s.skipped,
            "sub_errors": s.sub_errors,
            "mbytes": round(s.bytes / 1e6, 2),
            "last_notification_at": s.last_notification_at.isoformat() if s.last_notification_at else None,
        }

    # ---------- loop ----------

    async def run(self) -> None:
        backoff = self._t.reconnect_min_sec
        while not self._stopped:
            started = time.monotonic()
            try:
                async with websockets.connect(
                    self._t.ws_url,
                    max_size=2**22,
                    ping_interval=self._t.ping_interval_sec,
                    ping_timeout=self._t.ping_interval_sec,
                    open_timeout=15,
                ) as ws:
                    self._on_connect()
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, server closes, protocol errors
                self.stats.disconnects += 1
                self.stats.last_disconnect = f"{self._clock.now().isoformat()} {type(e).__name__}: {str(e)[:200]}"
                log.warning("trigger ws disconnected", extra=fields(error=self.stats.last_disconnect))
            finally:
                self._connected = False
                self._subs.clear()
                self._by_wallet.clear()
                self._pending.clear()
            if self._stopped:
                break
            if time.monotonic() - started > 60:
                backoff = self._t.reconnect_min_sec
            await self._clock.sleep(backoff * random.uniform(0.8, 1.2))
            backoff = min(backoff * 2, self._t.reconnect_max_sec)

    def _on_connect(self) -> None:
        self._connected = True
        self.stats.connects += 1
        self.stats.connected_since = self._clock.now()
        self._changed.set()
        log.info("trigger ws connected", extra=fields(url=self._t.ws_url, wallets=len(self._desired)))

    async def _session(self, ws) -> None:
        recv = asyncio.create_task(ws.recv())
        changed = asyncio.create_task(self._changed.wait())
        try:
            while not self._stopped:
                done, _ = await asyncio.wait({recv, changed}, return_when=asyncio.FIRST_COMPLETED)
                if changed in done:
                    self._changed.clear()
                    await self._sync(ws)
                    changed = asyncio.create_task(self._changed.wait())
                if recv in done:
                    raw = recv.result()  # raises on close → reconnect
                    self._handle(raw)
                    recv = asyncio.create_task(ws.recv())
        finally:
            for task in (recv, changed):
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    async def _sync(self, ws) -> None:
        busy = {w for k, w in self._pending.values() if k == "sub"}
        for w in sorted(self._desired - set(self._by_wallet) - busy):
            rid = self._req("sub", w)
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "method": "logsSubscribe",
                        "params": [{"mentions": [w]}, {"commitment": self._t.commitment}],
                    }
                )
            )
        for w in sorted(set(self._by_wallet) - self._desired):
            sid = self._by_wallet.pop(w)
            self._subs.pop(sid, None)
            rid = self._req("unsub", w)
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "logsUnsubscribe", "params": [sid]}))

    def _req(self, kind: str, wallet: str) -> int:
        rid = self._next_id
        self._next_id += 1
        self._pending[rid] = (kind, wallet)
        return rid

    def _handle(self, raw: str | bytes) -> None:
        self.stats.bytes += len(raw)
        try:
            m = json.loads(raw)
        except ValueError:
            return
        if "id" in m and m.get("id") in self._pending:
            kind, wallet = self._pending.pop(m["id"])
            if kind == "sub":
                if "result" in m and wallet in self._desired:
                    self._subs[m["result"]] = wallet
                    self._by_wallet[wallet] = m["result"]
                elif "result" not in m:
                    self.stats.sub_errors += 1
                    log.warning("trigger subscribe failed", extra=fields(wallet=wallet, error=str(m.get("error"))[:200]))
                    self._changed.set()
            return
        if m.get("method") != "logsNotification":
            return
        p = m.get("params") or {}
        wallet = self._subs.get(p.get("subscription"))
        res = p.get("result") or {}
        v = res.get("value") or {}
        sig = v.get("signature")
        if not wallet or not sig:
            return
        self.stats.notifications += 1
        now = self._clock.now()
        self.stats.last_notification_at = now
        if v.get("err") is not None:
            self.stats.failed_tx += 1
            return
        self.stats.ok_tx += 1
        swap = swap_like(v.get("logs"))
        self.sigs.put(sig, self._clock.monotonic(), swap, now)
        if not swap:
            self.stats.skipped += 1
            return
        self.stats.swap_like += 1
        self._on_trade(wallet, sig, int((res.get("context") or {}).get("slot") or 0))
