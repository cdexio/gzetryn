"""Watcher (spec §6): adaptive round-robin over active wallets; wallet_activity pages → ordered feed."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime

from gzetryn.clock import Clock
from gzetryn.config import WatchTunables
from gzetryn.core import watch as W
from gzetryn.gateway.gateway import Gateway, GatewayError
from gzetryn.gmgn import endpoints as E
from gzetryn.gmgn import parse
from gzetryn.log import fields, get_logger
from gzetryn.store.feed import FeedStore, WalletContext
from gzetryn.store.models import Wallet
from gzetryn.store.wallets import WalletStore, status_of

log = get_logger("gzetryn.watcher")


@dataclass
class WatchStats:
    polls_ok: int = 0
    polls_failed: int = 0
    pages: int = 0
    events_new: int = 0
    events_live: int = 0
    last_ok_at: datetime | None = None
    last_error: str | None = None
    tiers: dict[str, int] = field(default_factory=dict)


class Watcher:
    def __init__(
        self, t: WatchTunables, gateway: Gateway, wallets: WalletStore, feed: FeedStore, clock: Clock | None = None
    ):
        self._t = t
        self._gw = gateway
        self._wallets = wallets
        self._feed = feed
        self._clock = clock or Clock()
        self._ctx: dict[str, Wallet] = {}
        self._due: dict[str, float] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._next_reload = 0.0
        self._started = False
        self._stopped = False
        self.stats = WatchStats()

    def stop(self) -> None:
        self._stopped = True

    @property
    def active_count(self) -> int:
        return len(self._ctx)

    async def reload(self) -> None:
        rows = await self._wallets.active()
        now = self._clock.monotonic()
        fresh = {w.address: w for w in rows}
        for addr, w in fresh.items():
            if addr not in self._due:
                # spread the first round after a restart; a wallet added later is polled soon
                spread = self._t.first_spread_sec if not self._started else 5.0
                self._due[addr] = now + random.uniform(0, spread)
            old = self._ctx.get(addr)
            if old is not None and old.last_trade_at and (not w.last_trade_at or w.last_trade_at < old.last_trade_at):
                w.last_trade_at = old.last_trade_at  # in-memory value is newer than the row read
            self._ctx[addr] = w
        for addr in list(self._ctx):
            if addr not in fresh:
                self._ctx.pop(addr, None)
                self._due.pop(addr, None)
        self._started = True
        now_dt = self._clock.now()
        tiers: dict[str, int] = {}
        for w in self._ctx.values():
            k = W.tier(w.last_trade_at, now_dt, self._t)
            tiers[k] = tiers.get(k, 0) + 1
        self.stats.tiers = tiers

    async def run(self) -> None:
        while not self._stopped:
            mono = self._clock.monotonic()
            if mono >= self._next_reload:
                try:
                    await self.reload()
                except Exception:
                    log.exception("watcher reload failed")
                self._next_reload = mono + self._t.reload_sec
            for addr in self._pick(mono):
                self._inflight[addr] = asyncio.create_task(self._poll_guarded(addr), name=f"poll:{addr[:6]}")
            await self._clock.sleep(self._t.tick_sec)

    def _pick(self, mono: float) -> list[str]:
        free = self._t.max_concurrent - len(self._inflight)
        if free <= 0:
            return []
        due = sorted((d, a) for a, d in self._due.items() if d <= mono and a not in self._inflight)
        return [a for _, a in due[:free]]

    async def _poll_guarded(self, addr: str) -> None:
        try:
            await self.poll(addr)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poll crashed", extra=fields(wallet=addr))
            self._due[addr] = self._clock.monotonic() + self._t.cold_interval_sec
        finally:
            self._inflight.pop(addr, None)

    def _context(self, w: Wallet) -> WalletContext:
        return WalletContext(
            address=w.address,
            name=w.name,
            twitter_username=w.twitter_username,
            gmgn_tags=list(w.gmgn_tags or []),
            label=w.label,
            user_tags=list(w.user_tags or []),
            status=status_of(w),
            watch_started_at=w.watch_started_at,
        )

    async def poll(self, addr: str) -> int:
        w = self._ctx.get(addr)
        if w is None:
            return 0
        ctx = self._context(w)
        known_last = w.last_trade_at
        newest: datetime | None = None
        new_total = 0
        cursor: str | None = None
        try:
            for _ in range(self._t.max_pages):
                params = {"wallet": addr, "limit": self._t.page_limit}
                if cursor:
                    params["cursor"] = cursor
                r = await self._gw.call(E.WALLET_ACTIVITY, params=params, priority="P1")
                self.stats.pages += 1
                pg = parse.wallet_activity(r.body, addr)
                seen_at = self._clock.now()
                new = await self._feed.insert(ctx, pg.items, seen_at)
                new_total += len(new)
                live = [x for x in new if not W.is_baseline(x.trade_at, ctx.watch_started_at)]
                self.stats.events_live += len(live)
                if pg.items:
                    top = max(x.trade_at for x in pg.items)
                    newest = top if newest is None or top > newest else newest
                oldest = min((x.trade_at for x in pg.items), default=None)
                if not W.want_next_page(
                    self._t.page_limit, len(pg.items), len(new), oldest, known_last, bool(pg.next)
                ):
                    break
                cursor = pg.next
        except GatewayError as e:
            self.stats.polls_failed += 1
            self.stats.last_error = f"{addr[:8]}: {e}"
            await self._wallets.record_poll(addr, self._clock.now(), ok=False, error=str(e), last_trade_at=None)
            self._due[addr] = self._clock.monotonic() + max(W.interval(known_last, self._clock.now(), self._t), 60.0)
            return 0
        now = self._clock.now()
        if newest is not None and (known_last is None or newest > known_last):
            w.last_trade_at = newest
        await self._wallets.record_poll(addr, now, ok=True, error=None, last_trade_at=newest)
        self.stats.polls_ok += 1
        self.stats.events_new += new_total
        self.stats.last_ok_at = now
        base = W.interval(w.last_trade_at, now, self._t)
        self._due[addr] = self._clock.monotonic() + base * random.uniform(1 - self._t.jitter_frac, 1 + self._t.jitter_frac)
        if new_total:
            log.info("trades", extra=fields(wallet=addr, new=new_total, name=w.name))
        return new_total

    def summary(self) -> dict:
        s = self.stats
        return {
            "active_wallets": len(self._ctx),
            "in_flight": len(self._inflight),
            "tiers": s.tiers,
            "polls_ok": s.polls_ok,
            "polls_failed": s.polls_failed,
            "pages": s.pages,
            "events_new": s.events_new,
            "events_live": s.events_live,
            "last_ok_at": s.last_ok_at.isoformat() if s.last_ok_at else None,
            "last_error": s.last_error,
        }
