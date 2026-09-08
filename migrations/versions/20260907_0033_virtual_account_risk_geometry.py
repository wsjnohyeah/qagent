"""Add stop and target geometry to governed virtual-account risk revisions."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260907_0033"
down_revision: str | None = "20260907_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("virtual_accounts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "baseline_stop_fraction",
                sa.Numeric(12, 8),
                nullable=False,
                server_default="0.02",
            )
        )
        batch_op.add_column(
            sa.Column(
                "baseline_target_r_multiple",
                sa.Numeric(12, 8),
                nullable=False,
                server_default="2.00",
            )
        )
    with op.batch_alter_table("virtual_account_risk_revisions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "baseline_stop_fraction",
                sa.Numeric(12, 8),
                nullable=False,
                server_default="0.02",
            )
        )
        batch_op.add_column(
            sa.Column(
                "baseline_target_r_multiple",
                sa.Numeric(12, 8),
                nullable=False,
                server_default="2.00",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("virtual_account_risk_revisions") as batch_op:
        batch_op.drop_column("baseline_target_r_multiple")
        batch_op.drop_column("baseline_stop_fraction")
    with op.batch_alter_table("virtual_accounts") as batch_op:
        batch_op.drop_column("baseline_target_r_multiple")
        batch_op.drop_column("baseline_stop_fraction")
