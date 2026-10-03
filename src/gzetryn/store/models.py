"""Database tables (spec §9). Times are timestamptz in UTC; GMGN unix seconds are converted on parse."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


TS = DateTime(timezone=True)
ADDR = String(64)


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        Index("ix_wallets_active", "curated", "manual"),
        Index("ix_wallets_profit_30d", "realized_profit_30d"),
    )

    address: Mapped[str] = mapped_column(ADDR, primary_key=True)
    # identity (GMGN)
    name: Mapped[str | None] = mapped_column(String(128))
    twitter_username: Mapped[str | None] = mapped_column(String(64))
    twitter_name: Mapped[str | None] = mapped_column(String(128))
    twitter_fans: Mapped[int | None] = mapped_column(Integer)
    avatar: Mapped[str | None] = mapped_column(Text)
    gmgn_tags: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    rank_lists: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))  # ["kol:30d", ...] latest
    # curated (automatic) and manual are independent flags
    curated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    curated_rank: Mapped[int | None] = mapped_column(SmallInteger)
    curated_at: Mapped[datetime | None] = mapped_column(TS)
    uncurated_at: Mapped[datetime | None] = mapped_column(TS)
    manual: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    label: Mapped[str | None] = mapped_column(String(128))
    user_tags: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    note: Mapped[str | None] = mapped_column(Text)
    added_by: Mapped[str | None] = mapped_column(String(32))
    added_at: Mapped[datetime | None] = mapped_column(TS)
    # latest metrics (from the rank row, or walletNew for manual wallets outside the ranks)
    realized_profit_7d: Mapped[float | None] = mapped_column(Float)
    realized_profit_30d: Mapped[float | None] = mapped_column(Float)
    pnl_7d: Mapped[float | None] = mapped_column(Float)
    pnl_30d: Mapped[float | None] = mapped_column(Float)
    winrate_7d: Mapped[float | None] = mapped_column(Float)
    winrate_30d: Mapped[float | None] = mapped_column(Float)
    buy_30d: Mapped[int | None] = mapped_column(Integer)
    sell_30d: Mapped[int | None] = mapped_column(Integer)
    txs_30d: Mapped[int | None] = mapped_column(Integer)
    trades_per_day_30d: Mapped[float | None] = mapped_column(Float)
    avg_holding_sec_30d: Mapped[float | None] = mapped_column(Float)
    sol_balance: Mapped[float | None] = mapped_column(Float)
    follow_count: Mapped[int | None] = mapped_column(Integer)
    daily_profit_7d: Mapped[list | None] = mapped_column(JSONB)
    last_active_at: Mapped[datetime | None] = mapped_column(TS)
    metrics_at: Mapped[datetime | None] = mapped_column(TS)
    metrics_source: Mapped[str | None] = mapped_column(String(16))  # rank | wallet_new
    first_seen_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    last_ranked_at: Mapped[datetime | None] = mapped_column(TS)
    # watch state
    watch_started_at: Mapped[datetime | None] = mapped_column(TS)
    last_poll_at: Mapped[datetime | None] = mapped_column(TS)
    last_poll_ok_at: Mapped[datetime | None] = mapped_column(TS)
    last_poll_error: Mapped[str | None] = mapped_column(Text)
    last_trade_at: Mapped[datetime | None] = mapped_column(TS)
    polls_ok: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    polls_failed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now())


class RankSnapshot(Base):
    __tablename__ = "rank_snapshots"
    __table_args__ = (
        Index("ix_snap_address_at", "address", "snapshot_at"),
        Index("ix_snap_list_at", "period", "tag", "snapshot_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_at: Mapped[datetime] = mapped_column(TS)
    period: Mapped[str] = mapped_column(String(4))  # 7d | 30d
    tag: Mapped[str] = mapped_column(String(24))
    rank: Mapped[int] = mapped_column(SmallInteger)
    address: Mapped[str] = mapped_column(ADDR)
    realized_profit_7d: Mapped[float | None] = mapped_column(Float)
    realized_profit_30d: Mapped[float | None] = mapped_column(Float)
    pnl_7d: Mapped[float | None] = mapped_column(Float)
    pnl_30d: Mapped[float | None] = mapped_column(Float)
    winrate_7d: Mapped[float | None] = mapped_column(Float)
    winrate_30d: Mapped[float | None] = mapped_column(Float)
    buy_30d: Mapped[int | None] = mapped_column(Integer)
    sell_30d: Mapped[int | None] = mapped_column(Integer)
    txs_30d: Mapped[int | None] = mapped_column(Integer)
    sol_balance: Mapped[float | None] = mapped_column(Float)
    tags: Mapped[list | None] = mapped_column(JSONB)
    daily_profit_7d: Mapped[list | None] = mapped_column(JSONB)


class CurationRun(Base):
    __tablename__ = "curation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(TS, index=True)
    status: Mapped[str] = mapped_column(String(16))  # applied | skipped
    reason: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB)
    candidates: Mapped[int] = mapped_column(Integer)
    passed: Mapped[int] = mapped_column(Integer)
    rejected: Mapped[dict | None] = mapped_column(JSONB)  # counts per failed rule
    selected: Mapped[list] = mapped_column(JSONB)  # [{address, rank, realized_profit_30d, ...}]
    added: Mapped[list] = mapped_column(JSONB)
    removed: Mapped[list] = mapped_column(JSONB)


class Trade(Base):
    """The feed: one wallet buy/sell, seq commits in order (spec §6)."""

    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("wallet", "tx_hash", "mint", "side", "token_amount", name="uq_trades_natural"),
        Index("ix_trades_wallet_at", "wallet", "trade_at"),
        Index("ix_trades_mint_seq", "mint", "seq"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trade_at: Mapped[datetime] = mapped_column(TS)
    seen_at: Mapped[datetime] = mapped_column(TS)
    lag_sec: Mapped[float | None] = mapped_column(Float)
    wallet: Mapped[str] = mapped_column(ADDR)
    wallet_name: Mapped[str | None] = mapped_column(String(128))
    twitter_username: Mapped[str | None] = mapped_column(String(64))
    wallet_tags: Mapped[list | None] = mapped_column(JSONB)  # GMGN tags at event time
    label: Mapped[str | None] = mapped_column(String(128))
    user_tags: Mapped[list | None] = mapped_column(JSONB)
    wallet_status: Mapped[str | None] = mapped_column(String(16))  # curated | manual | curated+manual
    side: Mapped[str] = mapped_column(String(4))  # buy | sell
    mint: Mapped[str] = mapped_column(ADDR)
    symbol: Mapped[str | None] = mapped_column(String(64))
    token_amount: Mapped[float] = mapped_column(Float)
    sol_amount: Mapped[float | None] = mapped_column(Float)
    quote_symbol: Mapped[str | None] = mapped_column(String(16))
    usd_amount: Mapped[float | None] = mapped_column(Float)
    price_usd: Mapped[float | None] = mapped_column(Float)
    price_sol: Mapped[float | None] = mapped_column(Float)
    total_supply: Mapped[float | None] = mapped_column(Float)
    mcap_usd: Mapped[float | None] = mapped_column(Float)
    open_or_close: Mapped[bool | None] = mapped_column(Boolean)
    launchpad: Mapped[str | None] = mapped_column(String(32))
    launchpad_platform: Mapped[str | None] = mapped_column(String(32))
    tx_hash: Mapped[str] = mapped_column(String(100))
    baseline: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    payload: Mapped[dict | None] = mapped_column(JSONB)


class RequestLog(Base):
    __tablename__ = "request_log"
    __table_args__ = (
        UniqueConstraint("bucket_start", "consumer", "endpoint", "outcome", name="uq_request_log_bucket"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(TS, index=True)  # hour
    consumer: Mapped[str] = mapped_column(String(32))
    endpoint: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(16))
    count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    latency_ms_sum: Mapped[float] = mapped_column(Float, server_default=text("0"))


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(TS, index=True)
    endpoint: Mapped[str] = mapped_column(String(32))
    status: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(64))
    body: Mapped[str | None] = mapped_column(Text)
