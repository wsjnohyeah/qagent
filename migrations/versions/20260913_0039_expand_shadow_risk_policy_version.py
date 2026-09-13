"""Expand Shadow risk-policy lineage for deterministic dynamic geometry."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260913_0039"
down_revision: str | None = "20260912_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("shadow_risk_decisions") as batch_op:
        batch_op.alter_column(
            "policy_version",
            existing_type=sa.String(length=80),
            type_=sa.String(length=240),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("shadow_risk_decisions") as batch_op:
        batch_op.alter_column(
            "policy_version",
            existing_type=sa.String(length=240),
            type_=sa.String(length=80),
            existing_nullable=False,
        )
