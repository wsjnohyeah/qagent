"""Add isolated strategy Shadow sandbox lifecycle fields."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260911_0037"
down_revision: str | None = "20260909_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("shadow_deployments") as batch_op:
        batch_op.drop_constraint(
            "uq_shadow_deployments_strategy_symbol",
            type_="unique",
        )
        batch_op.add_column(
            sa.Column("liquidation_requested_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(sa.Column("retired_reason", sa.String(length=120)))
    op.create_index(
        "uq_shadow_deployments_nonterminal_strategy_symbol",
        "shadow_deployments",
        ["strategy_spec_id", "symbol"],
        unique=True,
        postgresql_where=sa.text(
            "status NOT IN ('RETIRED', 'RETIRED_LEGACY', "
            "'RETIRED_SHADOW_FAILED')"
        ),
        sqlite_where=sa.text(
            "status NOT IN ('RETIRED', 'RETIRED_LEGACY', "
            "'RETIRED_SHADOW_FAILED')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_shadow_deployments_nonterminal_strategy_symbol",
        table_name="shadow_deployments",
    )
    with op.batch_alter_table("shadow_deployments") as batch_op:
        batch_op.drop_column("retired_reason")
        batch_op.drop_column("liquidation_requested_at")
        batch_op.create_unique_constraint(
            "uq_shadow_deployments_strategy_symbol",
            ["strategy_spec_id", "symbol"],
        )
