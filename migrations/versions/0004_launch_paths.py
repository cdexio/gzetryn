"""launch_paths: pump.fun bonding-curve path per launch over its first window (phase 15, D-2026-10-06-01)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "launch_paths",
        sa.Column("mint", sa.String(64), primary_key=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("creator", sa.String(64)),
        sa.Column("name", sa.String(128)),
        sa.Column("symbol", sa.String(64)),
        sa.Column("mayhem", sa.Boolean),
        sa.Column("window_sec", sa.Integer, nullable=False),
        sa.Column("finalized_at", TS, nullable=False),
        sa.Column("trades", sa.Integer, nullable=False),
        sa.Column("buys", sa.Integer, nullable=False),
        sa.Column("sells", sa.Integer, nullable=False),
        sa.Column("buyers", sa.Integer, nullable=False),
        sa.Column("sellers", sa.Integer, nullable=False),
        sa.Column("sol_in", sa.Float, nullable=False),
        sa.Column("sol_out", sa.Float, nullable=False),
        sa.Column("dev_buy_sol", sa.Float, nullable=False),
        sa.Column("dev_sell_sol", sa.Float, nullable=False),
        sa.Column("dev_first_sell_sec", sa.Float),
        sa.Column("bundle_buyers", sa.Integer, nullable=False),
        sa.Column("bundle_sol", sa.Float, nullable=False),
        sa.Column("first_price_sol", sa.Float),
        sa.Column("last_price_sol", sa.Float),
        sa.Column("peak_price_sol", sa.Float),
        sa.Column("peak_sec", sa.Float),
        sa.Column("low_after_peak_sol", sa.Float),
        sa.Column("max_progress", sa.Float, nullable=False),
        sa.Column("checkpoints", JSONB, nullable=False),
        sa.Column("first_buyers", JSONB, nullable=False),
        sa.Column("completed_at", TS),
        sa.Column("migrated_pool", sa.String(64)),
    )
    op.create_index("ix_launch_paths_created", "launch_paths", ["created_at"])
    op.create_index("ix_launch_paths_creator", "launch_paths", ["creator"])


def downgrade() -> None:
    op.drop_table("launch_paths")
