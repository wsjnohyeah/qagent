"""Add robust validation diagnostics and research gate assessment."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0014"
down_revision: str | None = "20260904_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("validation_reports") as batch_op:
        batch_op.add_column(
            sa.Column(
                "robustness_metrics",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )
        batch_op.add_column(
            sa.Column(
                "gate_assessment",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )
    with op.batch_alter_table("validation_reports") as batch_op:
        batch_op.alter_column("robustness_metrics", server_default=None)
        batch_op.alter_column("gate_assessment", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("validation_reports") as batch_op:
        batch_op.drop_column("gate_assessment")
        batch_op.drop_column("robustness_metrics")
