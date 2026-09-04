"""Add event-driven backtest portfolio accounting."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0009"
down_revision: str | None = "20260904_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("backtest_trades") as batch_op:
        batch_op.add_column(
            sa.Column(
                "exit_quantity",
                sa.Numeric(precision=24, scale=10),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "corporate_action_cash",
                sa.Numeric(precision=24, scale=8),
                nullable=False,
                server_default="0",
            )
        )
    op.execute(
        sa.text(
            "UPDATE backtest_trades SET exit_quantity = quantity "
            "WHERE exit_quantity IS NULL"
        )
    )

    op.create_table(
        "backtest_portfolio_events",
        sa.Column("portfolio_event_id", sa.String(length=36), nullable=False),
        sa.Column("experiment_run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("cash_balance", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column(
            "position_quantity",
            sa.Numeric(precision=24, scale=10),
            nullable=False,
        ),
        sa.Column("cash_delta", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column(
            "quantity_delta",
            sa.Numeric(precision=24, scale=10),
            nullable=False,
        ),
        sa.Column("price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("corporate_action_id", sa.String(length=36), nullable=True),
        sa.Column("feature_snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_run_id"],
            ["experiment_runs.experiment_run_id"],
            name="fk_bt_events_experiment",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["corporate_action_id"],
            ["corporate_actions.corporate_action_id"],
            name="fk_bt_events_action",
        ),
        sa.ForeignKeyConstraint(
            ["feature_snapshot_id"],
            ["feature_snapshots.feature_snapshot_id"],
            name="fk_bt_events_feature",
        ),
        sa.PrimaryKeyConstraint(
            "portfolio_event_id",
            name="pk_backtest_portfolio_events",
        ),
        sa.UniqueConstraint(
            "experiment_run_id",
            "sequence",
            name="uq_backtest_portfolio_events_sequence",
        ),
    )
    op.create_index(
        "ix_backtest_portfolio_events_experiment_run_id",
        "backtest_portfolio_events",
        ["experiment_run_id"],
    )
    op.create_index(
        "ix_backtest_portfolio_events_event_type",
        "backtest_portfolio_events",
        ["event_type"],
    )
    op.create_index(
        "ix_backtest_portfolio_events_event_time",
        "backtest_portfolio_events",
        ["event_time"],
    )
    op.create_index(
        "ix_backtest_portfolio_events_symbol",
        "backtest_portfolio_events",
        ["symbol"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_backtest_portfolio_events_symbol",
        table_name="backtest_portfolio_events",
    )
    op.drop_index(
        "ix_backtest_portfolio_events_event_time",
        table_name="backtest_portfolio_events",
    )
    op.drop_index(
        "ix_backtest_portfolio_events_event_type",
        table_name="backtest_portfolio_events",
    )
    op.drop_index(
        "ix_backtest_portfolio_events_experiment_run_id",
        table_name="backtest_portfolio_events",
    )
    op.drop_table("backtest_portfolio_events")
    with op.batch_alter_table("backtest_trades") as batch_op:
        batch_op.drop_column("corporate_action_cash")
        batch_op.drop_column("exit_quantity")
