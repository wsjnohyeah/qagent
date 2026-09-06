"""Add confirmation-gated Alpaca paper trading and reconciliation state."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260906_0028"
down_revision: str | None = "20260906_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_enrollments",
        sa.Column("paper_enrollment_id", sa.String(length=36), nullable=False),
        sa.Column("shadow_deployment_id", sa.String(length=36), nullable=False),
        sa.Column("broker_account_id", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["shadow_deployment_id"],
            ["shadow_deployments.shadow_deployment_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("paper_enrollment_id"),
        sa.UniqueConstraint("shadow_deployment_id"),
    )
    op.create_index(
        "ix_paper_enrollments_shadow_deployment_id",
        "paper_enrollments",
        ["shadow_deployment_id"],
    )
    op.create_index(
        "ix_paper_enrollments_broker_account_id",
        "paper_enrollments",
        ["broker_account_id"],
    )
    op.create_index(
        "ix_paper_enrollments_status", "paper_enrollments", ["status"]
    )

    op.create_table(
        "paper_orders",
        sa.Column("paper_order_id", sa.String(length=36), nullable=False),
        sa.Column("paper_enrollment_id", sa.String(length=36), nullable=False),
        sa.Column("shadow_trade_plan_id", sa.String(length=36), nullable=False),
        sa.Column("client_order_id", sa.String(length=48), nullable=False),
        sa.Column("broker_order_id", sa.String(length=80), nullable=True),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 10), nullable=False),
        sa.Column("order_type", sa.String(length=24), nullable=False),
        sa.Column("time_in_force", sa.String(length=16), nullable=False),
        sa.Column("order_class", sa.String(length=24), nullable=False),
        sa.Column("entry_limit_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("take_profit_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("stop_loss_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("plan_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("lifecycle_complete", sa.Boolean(), nullable=False),
        sa.Column("submission_attempts", sa.Integer(), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(24, 10), nullable=False),
        sa.Column("filled_average_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("broker_payload_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["paper_enrollment_id"],
            ["paper_enrollments.paper_enrollment_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shadow_trade_plan_id"], ["shadow_trade_plans.trade_plan_id"]
        ),
        sa.PrimaryKeyConstraint("paper_order_id"),
        sa.UniqueConstraint("shadow_trade_plan_id"),
        sa.UniqueConstraint("client_order_id"),
        sa.UniqueConstraint("broker_order_id"),
    )
    for column in (
        "paper_enrollment_id",
        "shadow_trade_plan_id",
        "symbol",
        "status",
        "updated_at",
    ):
        op.create_index(f"ix_paper_orders_{column}", "paper_orders", [column])

    op.create_table(
        "paper_order_events",
        sa.Column("paper_order_event_id", sa.String(length=36), nullable=False),
        sa.Column("paper_order_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("broker_status", sa.String(length=40), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(24, 10), nullable=False),
        sa.Column("filled_average_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("lifecycle_complete", sa.Boolean(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["paper_order_id"],
            ["paper_orders.paper_order_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("paper_order_event_id"),
        sa.UniqueConstraint(
            "paper_order_id",
            "sequence",
            name="uq_paper_order_events_order_sequence",
        ),
    )
    for column in ("paper_order_id", "broker_status", "observed_at"):
        op.create_index(
            f"ix_paper_order_events_{column}", "paper_order_events", [column]
        )

    op.create_table(
        "paper_account_snapshots",
        sa.Column("paper_account_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("broker_account_id", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("cash", sa.Numeric(24, 8), nullable=False),
        sa.Column("buying_power", sa.Numeric(24, 8), nullable=False),
        sa.Column("equity", sa.Numeric(24, 8), nullable=False),
        sa.Column("portfolio_value", sa.Numeric(24, 8), nullable=False),
        sa.Column("last_equity", sa.Numeric(24, 8), nullable=False),
        sa.Column("pattern_day_trader", sa.Boolean(), nullable=False),
        sa.Column("trading_blocked", sa.Boolean(), nullable=False),
        sa.Column("transfers_blocked", sa.Boolean(), nullable=False),
        sa.Column("account_blocked", sa.Boolean(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("paper_account_snapshot_id"),
    )
    op.create_index(
        "ix_paper_account_snapshots_broker_account_id",
        "paper_account_snapshots",
        ["broker_account_id"],
    )
    op.create_index(
        "ix_paper_account_snapshots_observed_at",
        "paper_account_snapshots",
        ["observed_at"],
    )

    op.create_table(
        "paper_position_snapshots",
        sa.Column("paper_position_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("paper_account_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 10), nullable=False),
        sa.Column("average_entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("current_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("market_value", sa.Numeric(24, 8), nullable=False),
        sa.Column("cost_basis", sa.Numeric(24, 8), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(24, 8), nullable=False),
        sa.Column("unrealized_pnl_fraction", sa.Numeric(20, 10), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["paper_account_snapshot_id"],
            ["paper_account_snapshots.paper_account_snapshot_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("paper_position_snapshot_id"),
        sa.UniqueConstraint(
            "paper_account_snapshot_id",
            "symbol",
            name="uq_paper_position_snapshots_account_symbol",
        ),
    )
    for column in ("paper_account_snapshot_id", "symbol", "observed_at"):
        op.create_index(
            f"ix_paper_position_snapshots_{column}",
            "paper_position_snapshots",
            [column],
        )

    op.create_table(
        "paper_runs",
        sa.Column("paper_run_id", sa.String(length=36), nullable=False),
        sa.Column("trigger", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("enrollment_count", sa.Integer(), nullable=False),
        sa.Column("orders_submitted", sa.Integer(), nullable=False),
        sa.Column("orders_reconciled", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("paper_run_id"),
    )
    op.create_index("ix_paper_runs_status", "paper_runs", ["status"])
    op.create_index("ix_paper_runs_finished_at", "paper_runs", ["finished_at"])


def downgrade() -> None:
    op.drop_table("paper_runs")
    op.drop_table("paper_position_snapshots")
    op.drop_table("paper_account_snapshots")
    op.drop_table("paper_order_events")
    op.drop_table("paper_orders")
    op.drop_table("paper_enrollments")
