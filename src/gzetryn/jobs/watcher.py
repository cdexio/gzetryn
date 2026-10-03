"""Watcher (spec §6): GMGN wallet_activity pages → ordered feed.

Two ways a wallet gets polled:
- **trigger** (spec §6.1): the on-chain WebSocket saw a swap-like transaction of the wallet → poll ~1 s later,
  retry at +2/4/8/16 s until GMGN has indexed that signature (≤ 30 s). Per wallet ≥ 2 s apart, ≤ 10 per minute.
- **interval**: adaptive round-robin by the wallet's last trade (hot/warm/cold). While the trigger is healthy the
  slower fallback intervals apply; when it is down, the normal intervals.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from gzetryn.clock import Clock
from gzetryn.config import TriggerTunables, WatchTunables
from gzetryn.core import watch as W
from gzetryn.gateway.gateway import Gateway, GatewayError
from gzetryn.gmgn import endpoints as E
from gzetryn.gmgn import parse
from gzetryn.log import fields, get_logger
from gzetryn.store.feed import FeedStore, WalletContext
from gzetryn.store.models import Wallet
from gzetryn.store.wallets import WalletStore, status_of

log = get_logger("gzetryn.watcher")


def _pct(xs, p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(p * len(s)))], 2)


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
    interval_polls: int = 0
    # trigger side
    triggers: int = 0  # swap-like notifications for active wallets
    trigger_polls: int = 0
    trigger_hits: int = 0  # notified signature found in GMGN's page
    trigger_timeouts: int = 0  # not indexed by GMGN within max_wait (or not a buy/sell for GMGN)
    trigger_capped: int = 0  # per-wallet cap reached → left to fallback polling
    hit_after_sec: deque = field(default_factory=lambda: deque(maxlen=1000))  # notification → found in GMGN
    # coverage of new live trades by the trigger (only counted while the trigger was healthy)
    cov_notified: int = 0
    cov_filtered: int = 0  # notified but classified not swap-like → found later by an interval poll
    cov_not_notified: int = 0
    cov_trigger_down: int = 0


class Watcher:
    def __init__(
        self,
        t: WatchTunables,
        gateway: Gateway,
        wallets: WalletStore,
        feed: FeedStore,
        clock: Clock | None = None,
        trigger_t: TriggerTunables | None = None,
    ):
        self._t = t
        self._tt = trigger_t or TriggerTunables(enabled=False)
        self._gw = gateway
        self._wallets = wallets
        self._feed = feed
        self._clock = clock or Clock()
        self.trigger = None  # WsTrigger, attached by the runtime
        self._ctx: dict[str, Wallet] = {}
        self._due: dict[str, float] = {}
        self._trig_due: dict[str, float] = {}
        self._pending: dict[str, dict[str, tuple[float, float]]] = {}  # wallet → sig → (notified mono, deadline)
        self._attempt: dict[str, int] = {}
        self._trig_times: dict[str, deque] = {}
        self._last_trig_poll: dict[str, float] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._wake = asyncio.Event()
        self._next_reload = 0.0
        self._started = False
        self._stopped = False
        self._was_healthy = False
        self.stats = WatchStats()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    @property
    def active_count(self) -> int:
        return len(self._ctx)

    @property
    def _fallback(self) -> bool:
        return self.trigger is not None and self.trigger.healthy

    # ---------- active set ----------

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
                for d in (self._ctx, self._due, self._trig_due, self._pending, self._attempt, self._trig_times):
                    d.pop(addr, None)
        self._started = True
        if self.trigger is not None:
            self.trigger.set_wallets(set(self._ctx))
        now_dt = self._clock.now()
        tiers: dict[str, int] = {}
        for w in self._ctx.values():
            k = W.tier(w.last_trade_at, now_dt, self._t)
            tiers[k] = tiers.get(k, 0) + 1
        self.stats.tiers = tiers

    # ---------- trigger ----------

    def on_trade(self, wallet: str, signature: str, slot: int) -> None:
        """Called by the WsTrigger for a swap-like, successful transaction that mentions an active wallet."""
        if wallet not in self._ctx:
            return
        mono = self._clock.monotonic()
        self.stats.triggers += 1
        pend = self._pending.setdefault(wallet, {})
        if not pend:
            self._attempt[wallet] = 0
        pend[signature] = (mono, mono + self._tt.max_wait_sec)
        self._schedule_trigger(wallet, mono + self._tt.debounce_sec)

    def _schedule_trigger(self, wallet: str, at: float) -> bool:
        mono = self._clock.monotonic()
        times = self._trig_times.setdefault(wallet, deque())
        while times and mono - times[0] > 60.0:
            times.popleft()
        if len(times) >= self._tt.max_polls_per_min:
            self.stats.trigger_capped += 1
            return False
        at = max(at, self._last_trig_poll.get(wallet, -1e9) + self._tt.min_gap_sec)
        cur = self._trig_due.get(wallet)
        if cur is None or at < cur:
            self._trig_due[wallet] = at
        self._wake.set()
        return True

    def _resolve(self, wallet: str, found: set[str], mono: float, failed: bool = False) -> None:
        pend = self._pending.get(wallet)
        if not pend:
            return
        for sig in [s for s in pend if s in found]:
            notified, _ = pend.pop(sig)
            self.stats.trigger_hits += 1
            self.stats.hit_after_sec.append(mono - notified)
        for sig in [s for s, (_, dl) in pend.items() if mono >= dl]:
            pend.pop(sig)
            self.stats.trigger_timeouts += 1
        if not pend:
            self._pending.pop(wallet, None)
            self._attempt.pop(wallet, None)
            return
        n = self._attempt.get(wallet, 0) + 1
        self._attempt[wallet] = n
        delay = W.next_retry(n, self._tt.retry_delays_sec)
        if delay is None:
            self.stats.trigger_timeouts += len(pend)
            self._pending.pop(wallet, None)
            self._attempt.pop(wallet, None)
            return
        self._schedule_trigger(wallet, mono + delay)

    # ---------- loop ----------

    async def run(self) -> None:
        while not self._stopped:
            mono = self._clock.monotonic()
            if mono >= self._next_reload:
                try:
                    await self.reload()
                except Exception:
                    log.exception("watcher reload failed")
                self._next_reload = mono + self._t.reload_sec
            healthy = self._fallback
            if self._was_healthy and not healthy:
                # trigger went down: pull far-away fallback due times back to the normal intervals
                now_dt = self._clock.now()
                for addr, w in self._ctx.items():
                    cap = mono + W.interval(w.last_trade_at, now_dt, self._t) * random.uniform(0.2, 1.0)
                    if self._due.get(addr, 0) > cap:
                        self._due[addr] = cap
            self._was_healthy = healthy
            for addr, source in self._pick(mono):
                self._inflight[addr] = asyncio.create_task(self._poll_guarded(addr, source), name=f"poll:{addr[:6]}")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._t.tick_sec)

    def _pick(self, mono: float) -> list[tuple[str, str]]:
        free = self._t.max_concurrent - len(self._inflight)
        if free <= 0:
            return []
        out: list[tuple[str, str]] = []
        for d, a in sorted((d, a) for a, d in self._trig_due.items() if d <= mono and a not in self._inflight):
            if len(out) >= free:
                break
            del self._trig_due[a]
            out.append((a, "trigger"))
        taken = {a for a, _ in out}
        for _, a in sorted((d, a) for a, d in self._due.items() if d <= mono and a not in self._inflight):
            if len(out) >= free:
                break
            if a not in taken:
                out.append((a, "interval"))
        return out

    async def _poll_guarded(self, addr: str, source: str) -> None:
        try:
            await self.poll(addr, source)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poll crashed", extra=fields(wallet=addr))
            self._due[addr] = self._clock.monotonic() + self._t.cold_interval_sec
        finally:
            self._inflight.pop(addr, None)
            self._wake.set()

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

    def _annotate(self, rows: list[parse.TradeRow], source: str, mono: float) -> None:
        sigs = self.trigger.sigs if self.trigger is not None else None
        for r in rows:
            r.payload["source"] = source
            hit = sigs.get(r.tx_hash, mono) if sigs is not None else None
            if hit is not None:
                r.payload["notified"] = "swap" if hit[1] else "filtered"
                r.payload["notified_at"] = hit[2].isoformat()

    def _coverage(self, live: list[parse.TradeRow]) -> None:
        if self.trigger is None:
            return
        for r in live:
            n = r.payload.get("notified")
            if n == "swap":
                self.stats.cov_notified += 1
            elif n == "filtered":
                self.stats.cov_filtered += 1
            elif self.trigger.healthy:
                self.stats.cov_not_notified += 1
                log.info("trade not notified by the trigger", extra=fields(wallet=r.wallet, tx=r.tx_hash))
            else:
                self.stats.cov_trigger_down += 1

    async def poll(self, addr: str, source: str = "interval") -> int:
        w = self._ctx.get(addr)
        if w is None:
            return 0
        mono0 = self._clock.monotonic()
        if source == "trigger":
            self.stats.trigger_polls += 1
            self._last_trig_poll[addr] = mono0
            self._trig_times.setdefault(addr, deque()).append(mono0)
        else:
            self.stats.interval_polls += 1
        ctx = self._context(w)
        known_last = w.last_trade_at
        newest: datetime | None = None
        new_total = 0
        found: set[str] = set()
        cursor: str | None = None
        try:
            for _ in range(self._t.max_pages):
                params = {"wallet": addr, "limit": self._t.page_limit}
                if cursor:
                    params["cursor"] = cursor
                r = await self._gw.call(E.WALLET_ACTIVITY, params=params, priority="P1")
                self.stats.pages += 1
                pg = parse.wallet_activity(r.body, addr)
                found.update(x.tx_hash for x in pg.items)
                self._annotate(pg.items, source, self._clock.monotonic())
                seen_at = self._clock.now()
                new = await self._feed.insert(ctx, pg.items, seen_at)
                new_total += len(new)
                live = [x for x in new if not W.is_baseline(x.trade_at, ctx.watch_started_at)]
                self.stats.events_live += len(live)
                self._coverage(live)
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
            fb = self._fallback
            self._due[addr] = self._clock.monotonic() + max(
                W.interval(known_last, self._clock.now(), self._t, fallback=fb), 60.0
            )
            self._resolve(addr, set(), self._clock.monotonic())
            return 0
        now = self._clock.now()
        if newest is not None and (known_last is None or newest > known_last):
            w.last_trade_at = newest
        await self._wallets.record_poll(addr, now, ok=True, error=None, last_trade_at=newest)
        self.stats.polls_ok += 1
        self.stats.events_new += new_total
        self.stats.last_ok_at = now
        mono = self._clock.monotonic()
        base = W.interval(w.last_trade_at, now, self._t, fallback=self._fallback)
        self._due[addr] = mono + base * random.uniform(1 - self._t.jitter_frac, 1 + self._t.jitter_frac)
        self._resolve(addr, found, mono)
        if new_total:
            log.info("trades", extra=fields(wallet=addr, new=new_total, name=w.name, source=source))
        return new_total

    def summary(self) -> dict:
        s = self.stats
        out = {
            "active_wallets": len(self._ctx),
            "in_flight": len(self._inflight),
            "tiers": s.tiers,
            "mode": "fallback (trigger healthy)" if self._fallback else "interval",
            "polls_ok": s.polls_ok,
            "polls_failed": s.polls_failed,
            "interval_polls": s.interval_polls,
            "pages": s.pages,
            "events_new": s.events_new,
            "events_live": s.events_live,
            "last_ok_at": s.last_ok_at.isoformat() if s.last_ok_at else None,
            "last_error": s.last_error,
        }
        if self.trigger is not None:
            out["trigger"] = {
                **self.trigger.summary(),
                "triggers": s.triggers,
                "trigger_polls": s.trigger_polls,
                "hits": s.trigger_hits,
                "timeouts": s.trigger_timeouts,
                "capped": s.trigger_capped,
                "pending_wallets": len(self._pending),
                "found_after_sec_p50": _pct(s.hit_after_sec, 0.5),
                "found_after_sec_p90": _pct(s.hit_after_sec, 0.9),
                "coverage": {
                    "notified": s.cov_notified,
                    "filtered_not_swap": s.cov_filtered,
                    "not_notified": s.cov_not_notified,
                    "while_trigger_down": s.cov_trigger_down,
                },
            }
        return out
