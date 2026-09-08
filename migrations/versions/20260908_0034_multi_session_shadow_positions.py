"""Persist multi-session Shadow position state."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260908_0034"
down_revision: str | None = "20260907_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("shadow_trade_plans") as batch_op:
        batch_op.add_column(sa.Column("opened_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("entry_raw_price", sa.Numeric(20, 8)))
        batch_op.add_column(sa.Column("entry_fill_price", sa.Numeric(20, 8)))
        batch_op.add_column(sa.Column("entry_commission", sa.Numeric(24, 8)))
        batch_op.add_column(sa.Column("filled_quantity", sa.Numeric(24, 10)))
        batch_op.add_column(sa.Column("starting_cash", sa.Numeric(24, 8)))
        batch_op.add_column(sa.Column("corporate_action_cash", sa.Numeric(24, 8)))
        batch_op.add_column(sa.Column("applied_corporate_action_ids_json", sa.JSON()))


def downgrade() -> None:
    with op.batch_alter_table("shadow_trade_plans") as batch_op:
        batch_op.drop_column("applied_corporate_action_ids_json")
        batch_op.drop_column("corporate_action_cash")
        batch_op.drop_column("starting_cash")
        batch_op.drop_column("filled_quantity")
        batch_op.drop_column("entry_commission")
        batch_op.drop_column("entry_fill_price")
        batch_op.drop_column("entry_raw_price")
        batch_op.drop_column("opened_at")
