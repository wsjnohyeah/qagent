"""Persist Paper child orders and deterministic session-close exits."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260907_0032"
down_revision: str | None = "20260907_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_order_legs",
        sa.Column("paper_order_leg_id", sa.String(length=36), nullable=False),
        sa.Column("paper_order_id", sa.String(length=36), nullable=False),
        sa.Column("leg_role", sa.String(length=32), nullable=False),
        sa.Column("client_order_id", sa.String(length=48), nullable=True),
        sa.Column("broker_order_id", sa.String(length=80), nullable=True),
        sa.Column("parent_broker_order_id", sa.String(length=80), nullable=True),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("order_type", sa.String(length=24), nullable=False),
        sa.Column("time_in_force", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 10), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(24, 10), nullable=False),
        sa.Column("filled_average_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("limit_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("stop_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("lifecycle_complete", sa.Boolean(), nullable=False),
        sa.Column("submission_attempts", sa.Integer(), nullable=False),
        sa.Column("broker_payload_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["paper_order_id"],
            ["paper_orders.paper_order_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("paper_order_leg_id"),
        sa.UniqueConstraint("client_order_id"),
        sa.UniqueConstraint("broker_order_id"),
        sa.UniqueConstraint(
            "paper_order_id",
            "leg_role",
            name="uq_paper_order_legs_order_role",
        ),
    )
    for column in (
        "paper_order_id",
        "leg_role",
        "parent_broker_order_id",
        "symbol",
        "status",
        "updated_at",
    ):
        op.create_index(
            f"ix_paper_order_legs_{column}",
            "paper_order_legs",
            [column],
        )


def downgrade() -> None:
    op.drop_table("paper_order_legs")
