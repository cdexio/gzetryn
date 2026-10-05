"""candidates: drop kind=migrated rows that came from new_pairs (misclassified on 2026-10-05, see phase 8 report)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # a new pump_amm pool in GMGN's new pairs may be a direct PumpSwap launch: only the pump.fun `completed` list
    # is authoritative for migrations
    op.execute("delete from candidates where kind = 'migrated' and source = 'new_pairs'")


def downgrade() -> None:
    pass
