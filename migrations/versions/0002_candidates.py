"""candidates: normalized market candidates (pump.fun new/completing/migrated, new pairs, trending)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
ADDR = sa.String(64)


def upgrade() -> None:
    op.create_table(
        "candidates",
        sa.Column("seq", sa.BigInteger, primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("mint", ADDR, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("seen_count", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("symbol", sa.String(64)),
        sa.Column("name", sa.String(128)),
        sa.Column("pool_address", ADDR),
        sa.Column("exchange", sa.String(32)),
        sa.Column("launchpad", sa.String(32)),
        sa.Column("launchpad_platform", sa.String(32)),
        sa.Column("quote_address", ADDR),
        sa.Column("creator", ADDR),
        sa.Column("created_at", TS),
        sa.Column("open_at", TS),
        sa.Column("complete_at", TS),
        sa.Column("first", JSONB, nullable=False),
        sa.Column("last", JSONB, nullable=False),
        sa.UniqueConstraint("kind", "mint", name="uq_candidates_kind_mint"),
    )
    op.create_index("ix_candidates_kind_seq", "candidates", ["kind", "seq"])
    op.create_index("ix_candidates_first_seen", "candidates", ["first_seen_at"])


def downgrade() -> None:
    op.drop_table("candidates")
