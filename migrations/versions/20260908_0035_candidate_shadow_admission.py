"""Record qualified versus candidate-only Shadow admission."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260908_0035"
down_revision: str | None = "20260908_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("strategy_adoptions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "admission_tier",
                sa.String(length=32),
                nullable=False,
                server_default="QUALIFIED",
            )
        )
        batch_op.create_index(
            "ix_strategy_adoptions_admission_tier",
            ["admission_tier"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("strategy_adoptions") as batch_op:
        batch_op.drop_index("ix_strategy_adoptions_admission_tier")
        batch_op.drop_column("admission_tier")
