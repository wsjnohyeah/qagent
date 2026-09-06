"""Allow descriptive ML selection metric names in PostgreSQL."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260906_0029"
down_revision: str | None = "20260906_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ml_training_runs") as batch_op:
        batch_op.alter_column(
            "selection_metric",
            existing_type=sa.String(length=80),
            type_=sa.String(length=160),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("ml_training_runs") as batch_op:
        batch_op.alter_column(
            "selection_metric",
            existing_type=sa.String(length=160),
            type_=sa.String(length=80),
            existing_nullable=False,
        )
