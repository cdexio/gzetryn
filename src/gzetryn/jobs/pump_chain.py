"""pump.fun `completing` and `migrated` candidates from the chain (spec §7.2, phase 9).

Fed by the trigger's WebSocket connection (program-level `logsSubscribe` on the pump program). Per successful
transaction: CreateEvent → name/symbol cache; TradeEvent on a standard curve at or above the progress threshold →
`completing` row (first sighting at once, `last` updates at most every `update_sec`); CompletePumpAmmMigrationEvent →
`migrated` row with the PumpSwap pool. Rows go to the candidates store with `source = chain`.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime

from gzetryn.clock import Clock
from gzetryn.config import PumpChainTunables
from gzetryn.gmgn.parse import CANDIDATE_METRICS, CandidateRow
from gzetryn.log import fields, get_logger
from gzetryn.store.candidates import CandidateStore
from gzetryn.trigger import solana as S

log = get_logger("gzetryn.pump_chain")


def _pct(xs, p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(p * len(s)))], 2)


def _ts(sec: int | None) -> datetime | None:
    return datetime.fromtimestamp(sec, UTC) if sec and sec > 0 else None


class PumpChain:
    def __init__(self, t: PumpChainTunables, store: CandidateStore, sol_usd, clock: Clock | None = None):
        """`sol_usd`: async callable returning the current SOL/USD estimate (or None)."""
        self._t = t
        self._store = store
        self._sol_usd = sol_usd
        self._clock = clock or Clock()
        self._names: OrderedDict[str, S.Created] = OrderedDict()
        self._tracked: OrderedDict[str, float] = OrderedDict()  # mint → monotonic time of the last write
        self._curves: dict[str, str] = {}  # mint → bonding-curve PDA (computed once)
        self._migrated: OrderedDict[str, None] = OrderedDict()
        # (kind, mint) → (row, event block time, first sighting?)
        self._queue: dict[tuple[str, str], tuple[CandidateRow, float, bool]] = {}
        self._sol_price: tuple[float, float | None] = (-1e9, None)
        self._stopped = False
        self.paths = None  # LaunchPaths (phase 15), attached by the runtime: gets every create / trade / graduation
        self.last_tx_mono = -1e9
        self.fresh_new = deque(maxlen=2000)  # first sightings: row write time − event block time (s)
        self.fresh_updates = deque(maxlen=2000)
        self.stats = {
            "transactions": 0,
            "trades": 0,
            "decode_errors": 0,
            "creates": 0,
            "excluded_mayhem": 0,
            "excluded_nonstandard": 0,
            "completing_new": 0,
            "completing_updates": 0,
            "migrated_new": 0,
            "flushes": 0,
            "write_errors": 0,
            "last_write_at": None,
        }

    def stop(self) -> None:
        self._stopped = True

    # ---------- WS handler (sync, called on the event loop for every successful pump transaction) ----------

    def healthy(self, max_idle_sec: float) -> bool:
        """pump.fun transactions are arriving (the program stream carries ~10 MB/30 s, so idle = not subscribed)."""
        return self._clock.monotonic() - self.last_tx_mono <= max_idle_sec

    def on_logs(self, signature: str, slot: int, logs: list[str]) -> None:
        self.last_tx_mono = self._clock.monotonic()
        self.stats["transactions"] += 1
        paths = self.paths
        for kind, d in S.events_from_logs(logs):
            try:
                if kind == "trade":
                    t = S.decode_trade(d)
                    self._trade(t)
                    if paths is not None:
                        paths.on_trade(t, slot)
                elif kind == "create":
                    c = S.decode_create(d)
                    self._create(c)
                    if paths is not None:
                        paths.on_create(c, slot)
                elif kind == "migration":
                    m = S.decode_migration(d)
                    if self._t.migrated:
                        self._migration(m)
                    if paths is not None:
                        paths.on_complete(m.mint, m.timestamp, m.pool)
                elif kind == "complete":
                    mint, curve, ts = S.decode_complete(d)
                    if self._t.migrated:
                        self._tracked.pop(mint, None)
                    if paths is not None:
                        paths.on_complete(mint, ts)
            except (struct.error, IndexError, ValueError, KeyError):
                self.stats["decode_errors"] += 1

    def _create(self, c: S.Created) -> None:
        self.stats["creates"] += 1
        self._names[c.mint] = c
        while len(self._names) > self._t.names_cache:
            self._names.popitem(last=False)

    def _curve(self, mint: str) -> str | None:
        if mint not in self._curves:
            try:
                self._curves[mint] = S.bonding_curve_address(mint)
            except (KeyError, ValueError):
                return None
            if len(self._curves) > self._t.track_max * 2:
                self._curves.pop(next(iter(self._curves)))
        return self._curves[mint]

    def _trade(self, t: S.Trade) -> None:
        self.stats["trades"] += 1
        if t.mayhem_mode:
            self.stats["excluded_mayhem"] += 1
            return
        if not t.standard_curve:
            self.stats["excluded_nonstandard"] += 1
            return
        p = S.progress(t.real_token_reserves)
        if p < self._t.completing_min_progress or t.real_token_reserves == 0 or t.mint in self._migrated:
            return
        mono = self._clock.monotonic()
        last = self._tracked.get(t.mint)
        if last is not None and mono - last < self._t.update_sec and ("completing", t.mint) not in self._queue:
            return
        new = last is None
        self._tracked[t.mint] = mono
        self._tracked.move_to_end(t.mint)
        while len(self._tracked) > self._t.track_max:
            self._tracked.popitem(last=False)
        c = self._names.get(t.mint)
        metrics = dict.fromkeys(CANDIDATE_METRICS)
        metrics["progress"] = round(p, 6)
        metrics["_price_sol"] = t.price_sol  # converted to USD at flush time, then removed
        metrics["_real_sol"] = t.real_sol_reserves / 1e9 if t.sol_quote else None
        metrics["_supply"] = (c.token_total_supply if c and c.token_total_supply else S.STANDARD_TOTAL_SUPPLY) / 1e6
        row = CandidateRow(
            kind="completing",
            source="chain",
            mint=t.mint,
            symbol=c.symbol if c else None,
            name=c.name if c else None,
            pool_address=self._curve(t.mint),
            exchange="pump",
            launchpad="pump",
            launchpad_platform="Pump.fun",
            quote_address=t.quote_mint,
            creator=t.creator or (c.creator if c else None),
            created_at=_ts(c.timestamp) if c else None,
            metrics=metrics,
        )
        queued = self._queue.get(("completing", t.mint))
        self._queue[("completing", t.mint)] = (row, float(t.timestamp), new or bool(queued and queued[2]))
        self.stats["completing_new" if new else "completing_updates"] += 1

    def _migration(self, m: S.Migration) -> None:
        self._migrated[m.mint] = None
        while len(self._migrated) > self._t.track_max:
            self._migrated.popitem(last=False)
        self._tracked.pop(m.mint, None)
        self._queue.pop(("completing", m.mint), None)
        c = self._names.get(m.mint)
        metrics = dict.fromkeys(CANDIDATE_METRICS)
        metrics["progress"] = 1.0
        row = CandidateRow(
            kind="migrated",
            source="chain",
            mint=m.mint,
            symbol=c.symbol if c else None,
            name=c.name if c else None,
            pool_address=m.pool,
            exchange="pump_amm",
            launchpad="pump",
            launchpad_platform="Pump.fun",
            quote_address=m.quote_mint,
            creator=c.creator if c else None,
            created_at=_ts(c.timestamp) if c else None,
            open_at=_ts(m.timestamp),
            complete_at=_ts(m.timestamp),
            metrics=metrics,
        )
        self._queue[("migrated", m.mint)] = (row, float(m.timestamp), True)
        self.stats["migrated_new"] += 1

    # ---------- writer ----------

    async def _price(self) -> float | None:
        mono = self._clock.monotonic()
        at, v = self._sol_price
        if mono - at < 60 and v is not None:
            return v
        try:
            v = await self._sol_usd()
        except Exception:
            v = None
        self._sol_price = (mono, v)
        return v

    async def flush(self) -> int:
        if not self._queue:
            return 0
        batch = list(self._queue.values())
        self._queue.clear()
        sol_usd = await self._price()
        rows = []
        for row, block_ts, first in batch:
            m = row.metrics
            price_sol = m.pop("_price_sol", None)
            real_sol = m.pop("_real_sol", None)
            supply = m.pop("_supply", None)
            if sol_usd and price_sol is not None:
                m["price_usd"] = price_sol * sol_usd
                m["mcap_usd"] = m["price_usd"] * supply if supply else None
            if sol_usd and real_sol is not None:
                m["liquidity_usd"] = real_sol * sol_usd
            rows.append((row, block_ts, first))
        try:
            await self._store.upsert([r for r, _, _ in rows], self._clock.now())
        except Exception as e:
            self.stats["write_errors"] += 1
            log.warning("pump chain write failed", extra=fields(error=f"{type(e).__name__}: {str(e)[:200]}"))
            return 0
        now = time.time()
        for _, block_ts, first in rows:
            if block_ts > 0:
                (self.fresh_new if first else self.fresh_updates).append(max(0.0, now - block_ts))
        self.stats["flushes"] += 1
        self.stats["last_write_at"] = self._clock.now().isoformat()
        return len(rows)

    async def run(self) -> None:
        while not self._stopped:
            with contextlib.suppress(asyncio.CancelledError):
                await self._clock.sleep(self._t.flush_sec)
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pump chain flush failed")

    def summary(self) -> dict:
        return {
            **self.stats,
            "tracked": len(self._tracked),
            "names_cached": len(self._names),
            "queue": len(self._queue),
            "threshold": self._t.completing_min_progress,
            # event block time (whole seconds) → candidates row written
            "fresh_new_sec_p50": _pct(self.fresh_new, 0.5),
            "fresh_new_sec_p90": _pct(self.fresh_new, 0.9),
            "fresh_update_sec_p50": _pct(self.fresh_updates, 0.5),
            "fresh_update_sec_p90": _pct(self.fresh_updates, 0.9),
        }
