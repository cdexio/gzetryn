"""The trade feed (spec §6): idempotent inserts with seq committed in order, cursor reads with filters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gzetryn.core.watch import is_baseline
from gzetryn.gmgn.parse import TradeRow, iso
from gzetryn.store.models import Trade

FEED_LOCK = 7_301_993  # advisory lock key: serializes transactions that insert trades


@dataclass
class WalletContext:
    """Wallet facts frozen into each event at insert time."""

    address: str
    name: str | None
    twitter_username: str | None
    gmgn_tags: list
    label: str | None
    user_tags: list
    status: str | None
    watch_started_at: datetime | None


def trade_dict(t: Trade) -> dict:
    return {
        "seq": t.seq,
        "trade_at": iso(t.trade_at),
        "seen_at": iso(t.seen_at),
        "lag_sec": t.lag_sec,
        "wallet": t.wallet,
        "wallet_name": t.wallet_name,
        "twitter_username": t.twitter_username,
        "wallet_tags": t.wallet_tags or [],
        "label": t.label,
        "user_tags": t.user_tags or [],
        "wallet_status": t.wallet_status,
        "side": t.side,
        "mint": t.mint,
        "symbol": t.symbol,
        "token_amount": t.token_amount,
        "sol_amount": t.sol_amount,
        "quote_symbol": t.quote_symbol,
        "usd_amount": t.usd_amount,
        "price_usd": t.price_usd,
        "price_sol": t.price_sol,
        "total_supply": t.total_supply,
        "mcap_usd": t.mcap_usd,
        "liquidity_usd": None,  # not exposed per trade by GMGN (phase 0)
        "open_or_close": t.open_or_close,
        "launchpad": t.launchpad,
        "launchpad_platform": t.launchpad_platform,
        "tx_hash": t.tx_hash,
        "baseline": t.baseline,
        "payload": t.payload,
    }


class FeedStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def insert(self, ctx: WalletContext, rows: list[TradeRow], seen_at: datetime) -> list[TradeRow]:
        """Insert new trades (oldest first, so seq follows trade time); returns the rows that were new."""
        if not rows:
            return []
        values = []
        for r in sorted(rows, key=lambda x: (x.trade_at, x.tx_hash)):
            values.append(
                {
                    "trade_at": r.trade_at,
                    "seen_at": seen_at,
                    "lag_sec": max(0.0, (seen_at - r.trade_at).total_seconds()),
                    "wallet": ctx.address,
                    "wallet_name": ctx.name,
                    "twitter_username": ctx.twitter_username,
                    "wallet_tags": ctx.gmgn_tags,
                    "label": ctx.label,
                    "user_tags": ctx.user_tags,
                    "wallet_status": ctx.status,
                    "side": r.side,
                    "mint": r.mint,
                    "symbol": (r.symbol or "")[:64] or None,
                    "token_amount": r.token_amount,
                    "sol_amount": r.sol_amount,
                    "quote_symbol": (r.quote_symbol or "")[:16] or None,
                    "usd_amount": r.usd_amount,
                    "price_usd": r.price_usd,
                    "price_sol": r.price_sol,
                    "total_supply": r.total_supply,
                    "mcap_usd": r.mcap_usd,
                    "open_or_close": r.open_or_close,
                    "launchpad": (r.launchpad or "")[:32] or None,
                    "launchpad_platform": (r.launchpad_platform or "")[:32] or None,
                    "tx_hash": r.tx_hash,
                    "baseline": is_baseline(r.trade_at, ctx.watch_started_at),
                    "payload": r.payload,
                }
            )
        async with self._sessions() as s, s.begin():
            await s.execute(text("select pg_advisory_xact_lock(:k)"), {"k": FEED_LOCK})
            # skip trades we already have, so re-polled pages do not burn sequence values (ON CONFLICT still guards).
            # Matched on (tx_hash, mint, side) without the amount: a chain-decoded event (spec §6.2) and GMGN's later
            # row for the same trade differ in rounding, and must stay one event.
            known = {
                tuple(x)
                for x in (
                    await s.execute(
                        select(Trade.tx_hash, Trade.mint, Trade.side).where(
                            Trade.wallet == ctx.address, Trade.tx_hash.in_({v["tx_hash"] for v in values})
                        )
                    )
                ).all()
            }
            values = [v for v in values if (v["tx_hash"], v["mint"], v["side"]) not in known]
            if not values:
                return []
            stmt = (
                insert(Trade)
                .values(values)
                .on_conflict_do_nothing(constraint="uq_trades_natural")
                .returning(Trade.tx_hash, Trade.mint, Trade.side, Trade.token_amount)
            )
            new = {tuple(x) for x in (await s.execute(stmt)).all()}
        return [r for r in rows if (r.tx_hash, r.mint, r.side, r.token_amount) in new]

    async def read(
        self,
        after: int,
        *,
        wallet: str | None = None,
        mint: str | None = None,
        side: str | None = None,
        tag: str | None = None,
        include_baseline: bool = False,
        limit: int = 500,
    ) -> tuple[list[dict], int]:
        """Events after `after` and the next cursor (moves past filtered-out events; seq commits in order)."""
        async with self._sessions() as s:
            hi = (await s.execute(select(func.coalesce(func.max(Trade.seq), 0)))).scalar_one()
            q = select(Trade).where(Trade.seq > after, Trade.seq <= hi)
            if wallet:
                q = q.where(Trade.wallet == wallet)
            if mint:
                q = q.where(Trade.mint == mint)
            if side:
                q = q.where(Trade.side == side)
            if tag:
                q = q.where(or_(Trade.wallet_tags.contains([tag]), Trade.user_tags.contains([tag])))
            if not include_baseline:
                q = q.where(Trade.baseline.is_(False))
            q = q.order_by(Trade.seq).limit(limit)
            rows = [trade_dict(t) for t in (await s.execute(q)).scalars()]
        cursor = rows[-1]["seq"] if len(rows) >= limit else max(hi, after)
        return rows, cursor

    async def recent_sol_usd(self, window_min: int) -> float | None:
        """SOL/USD implied by GMGN's own recent trades (usd_amount / sol_amount), median over the window."""
        async with self._sessions() as s:
            q = (
                select(func.percentile_cont(0.5).within_group(Trade.usd_amount / Trade.sol_amount))
                .where(
                    Trade.seen_at >= func.now() - text(f"interval '{int(window_min)} minutes'"),
                    Trade.sol_amount > 0,
                    Trade.usd_amount > 0,
                    func.coalesce(Trade.payload["source"].astext, "gmgn") != "chain",
                )
            )
            v = (await s.execute(q)).scalar_one()
            return float(v) if v else None

    async def last_seq(self) -> int:
        async with self._sessions() as s:
            return (await s.execute(select(func.coalesce(func.max(Trade.seq), 0)))).scalar_one()

    async def for_mint(self, mint: str, limit: int) -> list[dict]:
        async with self._sessions() as s:
            q = select(Trade).where(Trade.mint == mint).order_by(Trade.seq.desc()).limit(limit)
            return [trade_dict(t) for t in (await s.execute(q)).scalars()]

    async def for_wallet(self, wallet: str, limit: int) -> list[dict]:
        async with self._sessions() as s:
            q = select(Trade).where(Trade.wallet == wallet).order_by(Trade.trade_at.desc()).limit(limit)
            return [trade_dict(t) for t in (await s.execute(q)).scalars()]

    async def stats(self, since: datetime) -> dict:
        async with self._sessions() as s:
            row = (
                await s.execute(
                    select(
                        func.count(),
                        func.count().filter(Trade.baseline.is_(False)),
                        func.count(func.distinct(Trade.wallet)),
                        func.max(Trade.seen_at),
                        func.percentile_cont(0.5).within_group(Trade.lag_sec).filter(Trade.baseline.is_(False)),
                        func.percentile_cont(0.9).within_group(Trade.lag_sec).filter(Trade.baseline.is_(False)),
                    ).where(Trade.seen_at >= since)
                )
            ).one()
            src = Trade.payload["source"].astext
            by_source = {
                (name or "unknown"): {
                    "events": n,
                    "lag_sec_p50": None if p50 is None else round(p50, 2),
                    "lag_sec_p90": None if p90 is None else round(p90, 2),
                }
                for name, n, p50, p90 in await s.execute(
                    select(
                        src,
                        func.count(),
                        func.percentile_cont(0.5).within_group(Trade.lag_sec),
                        func.percentile_cont(0.9).within_group(Trade.lag_sec),
                    )
                    .where(Trade.seen_at >= since, Trade.baseline.is_(False))
                    .group_by(src)
                )
            }
            return {
                "events": row[0],
                "live_events": row[1],
                "wallets": row[2],
                "last_seen_at": iso(row[3]),
                "lag_sec_p50": row[4],
                "lag_sec_p90": row[5],
                "live_by_source": by_source,
            }
