"""initial schema: wallets, rank_snapshots, curation_runs, trades, request_log, samples

Revision ID: 0001
Revises:
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
ADDR = sa.String(64)
EMPTY = sa.text("'[]'::jsonb")
FALSE = sa.text("false")
ZERO = sa.text("0")


def upgrade() -> None:
    op.create_table(
        "wallets",
        sa.Column("address", ADDR, primary_key=True),
        sa.Column("name", sa.String(128)),
        sa.Column("twitter_username", sa.String(64)),
        sa.Column("twitter_name", sa.String(128)),
        sa.Column("twitter_fans", sa.Integer),
        sa.Column("avatar", sa.Text),
        sa.Column("gmgn_tags", JSONB, nullable=False, server_default=EMPTY),
        sa.Column("rank_lists", JSONB, nullable=False, server_default=EMPTY),
        sa.Column("curated", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("curated_rank", sa.SmallInteger),
        sa.Column("curated_at", TS),
        sa.Column("uncurated_at", TS),
        sa.Column("manual", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("label", sa.String(128)),
        sa.Column("user_tags", JSONB, nullable=False, server_default=EMPTY),
        sa.Column("note", sa.Text),
        sa.Column("added_by", sa.String(32)),
        sa.Column("added_at", TS),
        sa.Column("realized_profit_7d", sa.Float),
        sa.Column("realized_profit_30d", sa.Float),
        sa.Column("pnl_7d", sa.Float),
        sa.Column("pnl_30d", sa.Float),
        sa.Column("winrate_7d", sa.Float),
        sa.Column("winrate_30d", sa.Float),
        sa.Column("buy_30d", sa.Integer),
        sa.Column("sell_30d", sa.Integer),
        sa.Column("txs_30d", sa.Integer),
        sa.Column("trades_per_day_30d", sa.Float),
        sa.Column("avg_holding_sec_30d", sa.Float),
        sa.Column("sol_balance", sa.Float),
        sa.Column("follow_count", sa.Integer),
        sa.Column("daily_profit_7d", JSONB),
        sa.Column("last_active_at", TS),
        sa.Column("metrics_at", TS),
        sa.Column("metrics_source", sa.String(16)),
        sa.Column("first_seen_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("last_ranked_at", TS),
        sa.Column("watch_started_at", TS),
        sa.Column("last_poll_at", TS),
        sa.Column("last_poll_ok_at", TS),
        sa.Column("last_poll_error", sa.Text),
        sa.Column("last_trade_at", TS),
        sa.Column("polls_ok", sa.Integer, nullable=False, server_default=ZERO),
        sa.Column("polls_failed", sa.Integer, nullable=False, server_default=ZERO),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_wallets_active", "wallets", ["curated", "manual"])
    op.create_index("ix_wallets_profit_30d", "wallets", ["realized_profit_30d"])

    op.create_table(
        "rank_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("snapshot_at", TS, nullable=False),
        sa.Column("period", sa.String(4), nullable=False),
        sa.Column("tag", sa.String(24), nullable=False),
        sa.Column("rank", sa.SmallInteger, nullable=False),
        sa.Column("address", ADDR, nullable=False),
        sa.Column("realized_profit_7d", sa.Float),
        sa.Column("realized_profit_30d", sa.Float),
        sa.Column("pnl_7d", sa.Float),
        sa.Column("pnl_30d", sa.Float),
        sa.Column("winrate_7d", sa.Float),
        sa.Column("winrate_30d", sa.Float),
        sa.Column("buy_30d", sa.Integer),
        sa.Column("sell_30d", sa.Integer),
        sa.Column("txs_30d", sa.Integer),
        sa.Column("sol_balance", sa.Float),
        sa.Column("tags", JSONB),
        sa.Column("daily_profit_7d", JSONB),
    )
    op.create_index("ix_snap_address_at", "rank_snapshots", ["address", "snapshot_at"])
    op.create_index("ix_snap_list_at", "rank_snapshots", ["period", "tag", "snapshot_at"])

    op.create_table(
        "curation_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("at", TS, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("candidates", sa.Integer, nullable=False),
        sa.Column("passed", sa.Integer, nullable=False),
        sa.Column("rejected", JSONB),
        sa.Column("selected", JSONB, nullable=False),
        sa.Column("added", JSONB, nullable=False),
        sa.Column("removed", JSONB, nullable=False),
    )
    op.create_index("ix_curation_runs_at", "curation_runs", ["at"])

    op.create_table(
        "trades",
        sa.Column("seq", sa.BigInteger, primary_key=True),
        sa.Column("trade_at", TS, nullable=False),
        sa.Column("seen_at", TS, nullable=False),
        sa.Column("lag_sec", sa.Float),
        sa.Column("wallet", ADDR, nullable=False),
        sa.Column("wallet_name", sa.String(128)),
        sa.Column("twitter_username", sa.String(64)),
        sa.Column("wallet_tags", JSONB),
        sa.Column("label", sa.String(128)),
        sa.Column("user_tags", JSONB),
        sa.Column("wallet_status", sa.String(16)),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("mint", ADDR, nullable=False),
        sa.Column("symbol", sa.String(64)),
        sa.Column("token_amount", sa.Float, nullable=False),
        sa.Column("sol_amount", sa.Float),
        sa.Column("quote_symbol", sa.String(16)),
        sa.Column("usd_amount", sa.Float),
        sa.Column("price_usd", sa.Float),
        sa.Column("price_sol", sa.Float),
        sa.Column("total_supply", sa.Float),
        sa.Column("mcap_usd", sa.Float),
        sa.Column("open_or_close", sa.Boolean),
        sa.Column("launchpad", sa.String(32)),
        sa.Column("launchpad_platform", sa.String(32)),
        sa.Column("tx_hash", sa.String(100), nullable=False),
        sa.Column("baseline", sa.Boolean, nullable=False, server_default=FALSE),
        sa.Column("payload", JSONB),
        sa.UniqueConstraint("wallet", "tx_hash", "mint", "side", "token_amount", name="uq_trades_natural"),
    )
    op.create_index("ix_trades_wallet_at", "trades", ["wallet", "trade_at"])
    op.create_index("ix_trades_mint_seq", "trades", ["mint", "seq"])

    op.create_table(
        "request_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("bucket_start", TS, nullable=False),
        sa.Column("consumer", sa.String(32), nullable=False),
        sa.Column("endpoint", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("count", sa.Integer, nullable=False, server_default=ZERO),
        sa.Column("latency_ms_sum", sa.Float, nullable=False, server_default=ZERO),
        sa.UniqueConstraint("bucket_start", "consumer", "endpoint", "outcome", name="uq_request_log_bucket"),
    )
    op.create_index("ix_request_log_bucket_start", "request_log", ["bucket_start"])

    op.create_table(
        "samples",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("at", TS, nullable=False),
        sa.Column("endpoint", sa.String(32), nullable=False),
        sa.Column("status", sa.Integer),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("body", sa.Text),
    )
    op.create_index("ix_samples_at", "samples", ["at"])


def downgrade() -> None:
    for t in ("samples", "request_log", "trades", "curation_runs", "rank_snapshots", "wallets"):
        op.drop_table(t)
