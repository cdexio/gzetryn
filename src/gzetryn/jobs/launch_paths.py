"""Curve path recorder (phase 15, engine research G1, D-2026-10-06-01).

Every pump.fun launch whose CreateEvent arrives on the pump program stream is followed for `window_sec`: trades,
buyers, dev (creator) trades, same-slot bundle buys, the price at fixed offsets, the peak, the low after the peak
and the curve progress. At the end of the window the launch becomes one `launch_paths` row. A graduation
(CompleteEvent / migration) is recorded on the row even when it comes after the window.

Fed synchronously by PumpChain.on_logs (no extra connection, no RPC); rows are written by `run()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from gzetryn.clock import Clock
from gzetryn.config import LaunchPathsTunables
from gzetryn.log import fields, get_logger
from gzetryn.store.launch_paths import LaunchPathStore
from gzetryn.trigger import solana as S

log = get_logger("gzetryn.launch_paths")


def _dt(sec: float | None) -> datetime | None:
    return datetime.fromtimestamp(sec, UTC) if sec and sec > 0 else None


@dataclass
class Path:
    mint: str
    created_ts: int  # CreateEvent block time (unix s)
    create_slot: int
    creator: str | None
    name: str | None
    symbol: str | None
    mayhem: bool | None
    trades: int = 0
    buys: int = 0
    sells: int = 0
    buyers: set = field(default_factory=set)
    sellers: set = field(default_factory=set)
    sol_in: float = 0.0
    sol_out: float = 0.0
    dev_buy_sol: float = 0.0
    dev_sell_sol: float = 0.0
    dev_first_sell_sec: float | None = None
    bundle: set = field(default_factory=set)
    bundle_sol: float = 0.0
    first_price: float | None = None
    last_price: float | None = None
    peak_price: float | None = None
    peak_sec: float | None = None
    low_after_peak: float | None = None
    max_progress: float = 0.0
    checkpoints: dict = field(default_factory=dict)  # offset (s) → price
    first_buyers: list = field(default_factory=list)
    completed_ts: float | None = None
    pool: str | None = None


class LaunchPaths:
    def __init__(self, t: LaunchPathsTunables, store: LaunchPathStore, clock: Clock | None = None):
        self._t = t
        self._store = store
        self._clock = clock or Clock()
        self._paths: OrderedDict[str, Path] = OrderedDict()  # in creation order
        self._done: list[dict] = []
        self._late: dict[str, tuple[float | None, str | None]] = {}  # finalized mint → (completed ts, pool)
        self._finalized: OrderedDict[str, None] = OrderedDict()  # recent finalized mints (late graduations)
        self._stopped = False
        self.stats = {"created": 0, "trades": 0, "finalized": 0, "early_finalized": 0, "written": 0,
                      "late_completions": 0, "write_errors": 0, "last_write_at": None}

    def stop(self) -> None:
        self._stopped = True

    # ---------- events (sync, from PumpChain.on_logs) ----------

    def on_create(self, c: S.Created, slot: int) -> None:
        if c.mint in self._paths or c.timestamp <= 0:
            return
        self._paths[c.mint] = Path(c.mint, c.timestamp, slot, c.creator, c.name, c.symbol, c.mayhem_mode)
        self.stats["created"] += 1
        while len(self._paths) > self._t.max_in_flight:
            _, oldest = self._paths.popitem(last=False)
            self._finalize(oldest)
            self.stats["early_finalized"] += 1

    def on_trade(self, t: S.Trade, slot: int) -> None:
        p = self._paths.get(t.mint)
        if p is None:
            return
        sec = float(t.timestamp - p.created_ts)
        if sec > self._t.window_sec:
            return  # finalized on the next sweep; the window is closed
        self.stats["trades"] += 1
        price = t.price_sol
        for c in self._t.checkpoints_sec:  # the price at an offset = the last trade price at or before it
            if c < sec and c not in p.checkpoints:
                p.checkpoints[c] = p.last_price
        p.trades += 1
        sol = t.sol_amount / 1e9 if t.sol_quote else 0.0
        dev = t.user is not None and t.user == p.creator
        if t.is_buy:
            p.buys += 1
            p.sol_in += sol
            if dev:
                p.dev_buy_sol += sol
            elif t.user:
                if len(p.buyers) < self._t.max_tracked_traders:
                    p.buyers.add(t.user)
                if slot and slot <= p.create_slot + self._t.bundle_slots and t.user not in p.bundle:
                    p.bundle.add(t.user)
                    p.bundle_sol += sol
                if len(p.first_buyers) < self._t.first_buyers and all(b["w"] != t.user for b in p.first_buyers):
                    p.first_buyers.append({"w": t.user, "sec": sec, "sol": round(sol, 6)})
        else:
            p.sells += 1
            p.sol_out += sol
            if dev:
                p.dev_sell_sol += sol
                if p.dev_first_sell_sec is None:
                    p.dev_first_sell_sec = sec
            elif t.user and len(p.sellers) < self._t.max_tracked_traders:
                p.sellers.add(t.user)
        if price is not None:
            if p.first_price is None:
                p.first_price = price
            p.last_price = price
            if p.peak_price is None or price > p.peak_price:
                p.peak_price, p.peak_sec, p.low_after_peak = price, sec, price
            elif p.low_after_peak is None or price < p.low_after_peak:
                p.low_after_peak = price
        if t.standard_curve:
            p.max_progress = max(p.max_progress, S.progress(t.real_token_reserves))

    def on_complete(self, mint: str, ts: int | None = None, pool: str | None = None) -> None:
        """CompleteEvent (pool None) or migration (pool set): graduation of a followed or recently finalized mint."""
        p = self._paths.get(mint)
        if p is not None:
            p.completed_ts = p.completed_ts or (float(ts) if ts else time.time())
            p.max_progress = 1.0
            p.pool = p.pool or pool
            return
        if mint in self._finalized:
            done_ts, done_pool = self._late.get(mint, (None, None))
            self._late[mint] = (done_ts or (float(ts) if ts else None), done_pool or pool)

    # ---------- finalize and write ----------

    def _finalize(self, p: Path) -> None:
        elapsed = min(float(self._t.window_sec), time.time() - p.created_ts)
        for c in self._t.checkpoints_sec:
            if c not in p.checkpoints and c <= elapsed:
                p.checkpoints[c] = p.last_price
        self._done.append(
            {
                "mint": p.mint,
                "created_at": _dt(p.created_ts),
                "creator": p.creator,
                "name": (p.name or "")[:128] or None,
                "symbol": (p.symbol or "")[:64] or None,
                "mayhem": p.mayhem,
                "window_sec": self._t.window_sec,
                "finalized_at": self._clock.now(),
                "trades": p.trades,
                "buys": p.buys,
                "sells": p.sells,
                "buyers": len(p.buyers),
                "sellers": len(p.sellers),
                "sol_in": round(p.sol_in, 9),
                "sol_out": round(p.sol_out, 9),
                "dev_buy_sol": round(p.dev_buy_sol, 9),
                "dev_sell_sol": round(p.dev_sell_sol, 9),
                "dev_first_sell_sec": p.dev_first_sell_sec,
                "bundle_buyers": len(p.bundle),
                "bundle_sol": round(p.bundle_sol, 9),
                "first_price_sol": p.first_price,
                "last_price_sol": p.last_price,
                "peak_price_sol": p.peak_price,
                "peak_sec": p.peak_sec,
                "low_after_peak_sol": p.low_after_peak,
                "max_progress": round(p.max_progress, 6),
                "checkpoints": {str(k): v for k, v in sorted(p.checkpoints.items())},
                "first_buyers": p.first_buyers,
                "completed_at": _dt(p.completed_ts),
                "migrated_pool": p.pool,
            }
        )
        self._finalized[p.mint] = None
        while len(self._finalized) > self._t.max_in_flight * 4:
            self._finalized.popitem(last=False)
        self.stats["finalized"] += 1

    def sweep(self, now: float | None = None) -> int:
        """Finalize every launch whose window has passed (creation order, so stop at the first open one)."""
        now = time.time() if now is None else now
        n = 0
        while self._paths:
            mint, p = next(iter(self._paths.items()))
            if now - p.created_ts < self._t.window_sec:
                break
            self._paths.pop(mint)
            self._finalize(p)
            n += 1
        return n

    async def flush(self) -> int:
        self.sweep()
        rows, self._done = self._done, []
        late, self._late = self._late, {}
        written = 0
        try:
            if rows:
                written = await self._store.insert(rows)
            for mint, (ts, pool) in late.items():
                self.stats["late_completions"] += await self._store.mark_completed(mint, _dt(ts), pool)
        except Exception as e:
            self.stats["write_errors"] += 1
            log.warning("launch path write failed", extra=fields(error=f"{type(e).__name__}: {str(e)[:200]}"))
            return 0
        if rows:
            self.stats["written"] += written
            self.stats["last_write_at"] = self._clock.now().isoformat()
        return written

    async def run(self) -> None:
        while not self._stopped:
            with contextlib.suppress(asyncio.CancelledError):
                await self._clock.sleep(self._t.flush_sec)
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("launch path flush failed")

    def summary(self) -> dict:
        return {**self.stats, "in_flight": len(self._paths), "pending_rows": len(self._done),
                "window_sec": self._t.window_sec}
