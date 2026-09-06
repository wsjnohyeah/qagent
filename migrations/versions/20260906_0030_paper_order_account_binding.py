"""Bind every Paper order intent to one broker account."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260906_0030"
down_revision: str | None = "20260906_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "paper_orders",
        sa.Column("broker_account_id", sa.String(length=80), nullable=True),
    )
    op.create_index(
        "ix_paper_orders_broker_account_id",
        "paper_orders",
        ["broker_account_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_paper_orders_broker_account_id", table_name="paper_orders")
    op.drop_column("paper_orders", "broker_account_id")
