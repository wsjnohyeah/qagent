"""Add the shared virtual master account, sleeves, and atomic reservations."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa


revision: str = "20260906_0027"
down_revision: str | None = "20260906_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAIN_ACCOUNT_ID = "cffdf661-b2c0-5e58-a49c-13f8a2403d98"


def upgrade() -> None:
    op.create_table(
        "virtual_accounts",
        sa.Column("virtual_account_id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("initial_cash", sa.Numeric(24, 8), nullable=False),
        sa.Column("cash_balance", sa.Numeric(24, 8), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(24, 8), nullable=False),
        sa.Column("reserved_cash", sa.Numeric(24, 8), nullable=False),
        sa.Column("reserved_risk_usd", sa.Numeric(24, 8), nullable=False),
        sa.Column("base_policy_version", sa.String(length=80), nullable=False),
        sa.Column("risk_revision", sa.Integer(), nullable=False),
        sa.Column("initial_risk_fraction", sa.Numeric(12, 8), nullable=False),
        sa.Column("maximum_trade_risk_usd", sa.Numeric(24, 8), nullable=False),
        sa.Column("maximum_concurrent_risk_usd", sa.Numeric(24, 8), nullable=False),
        sa.Column("daily_loss_stop_usd", sa.Numeric(24, 8), nullable=False),
        sa.Column("account_floor_usd", sa.Numeric(24, 8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("virtual_account_id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_virtual_accounts_status", "virtual_accounts", ["status"])
    op.create_index(
        "ix_virtual_accounts_updated_at", "virtual_accounts", ["updated_at"]
    )
    op.create_table(
        "virtual_account_risk_revisions",
        sa.Column("risk_revision_id", sa.String(length=36), nullable=False),
        sa.Column("virtual_account_id", sa.String(length=36), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("initial_risk_fraction", sa.Numeric(12, 8), nullable=False),
        sa.Column("maximum_trade_risk_usd", sa.Numeric(24, 8), nullable=False),
        sa.Column("maximum_concurrent_risk_usd", sa.Numeric(24, 8), nullable=False),
        sa.Column("daily_loss_stop_usd", sa.Numeric(24, 8), nullable=False),
        sa.Column("account_floor_usd", sa.Numeric(24, 8), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["virtual_account_id"],
            ["virtual_accounts.virtual_account_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("risk_revision_id"),
        sa.UniqueConstraint(
            "virtual_account_id",
            "revision_number",
            name="uq_virtual_account_risk_revisions_number",
        ),
    )
    op.create_index(
        "ix_virtual_account_risk_revisions_virtual_account_id",
        "virtual_account_risk_revisions",
        ["virtual_account_id"],
    )
    op.create_table(
        "strategy_sleeves",
        sa.Column("strategy_sleeve_id", sa.String(length=36), nullable=False),
        sa.Column("virtual_account_id", sa.String(length=36), nullable=False),
        sa.Column("strategy_spec_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["virtual_account_id"],
            ["virtual_accounts.virtual_account_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["strategy_spec_id"], ["strategy_specs.strategy_spec_id"]),
        sa.PrimaryKeyConstraint("strategy_sleeve_id"),
        sa.UniqueConstraint(
            "virtual_account_id",
            "strategy_spec_id",
            "symbol",
            name="uq_strategy_sleeves_account_strategy_symbol",
        ),
    )
    op.create_index(
        "ix_strategy_sleeves_virtual_account_id",
        "strategy_sleeves",
        ["virtual_account_id"],
    )
    op.create_index(
        "ix_strategy_sleeves_strategy_spec_id",
        "strategy_sleeves",
        ["strategy_spec_id"],
    )
    op.create_index("ix_strategy_sleeves_symbol", "strategy_sleeves", ["symbol"])
    op.create_index("ix_strategy_sleeves_status", "strategy_sleeves", ["status"])

    now = datetime.now(UTC)
    accounts = sa.table(
        "virtual_accounts",
        sa.column("virtual_account_id", sa.String),
        sa.column("slug", sa.String),
        sa.column("name", sa.String),
        sa.column("account_type", sa.String),
        sa.column("currency", sa.String),
        sa.column("status", sa.String),
        sa.column("initial_cash", sa.Numeric),
        sa.column("cash_balance", sa.Numeric),
        sa.column("realized_pnl", sa.Numeric),
        sa.column("reserved_cash", sa.Numeric),
        sa.column("reserved_risk_usd", sa.Numeric),
        sa.column("base_policy_version", sa.String),
        sa.column("risk_revision", sa.Integer),
        sa.column("initial_risk_fraction", sa.Numeric),
        sa.column("maximum_trade_risk_usd", sa.Numeric),
        sa.column("maximum_concurrent_risk_usd", sa.Numeric),
        sa.column("daily_loss_stop_usd", sa.Numeric),
        sa.column("account_floor_usd", sa.Numeric),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    op.bulk_insert(
        accounts,
        [
            {
                "virtual_account_id": MAIN_ACCOUNT_ID,
                "slug": "shadow-main",
                "name": "Shared Shadow Master",
                "account_type": "SHARED_MASTER",
                "currency": "USD",
                "status": "ACTIVE",
                "initial_cash": 100000,
                "cash_balance": 100000,
                "realized_pnl": 0,
                "reserved_cash": 0,
                "reserved_risk_usd": 0,
                "base_policy_version": "risk_policy@0.3.0",
                "risk_revision": 1,
                "initial_risk_fraction": 0.0025,
                "maximum_trade_risk_usd": 130,
                "maximum_concurrent_risk_usd": 780,
                "daily_loss_stop_usd": 520,
                "account_floor_usd": 40000,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    revisions = sa.table(
        "virtual_account_risk_revisions",
        sa.column("risk_revision_id", sa.String),
        sa.column("virtual_account_id", sa.String),
        sa.column("revision_number", sa.Integer),
        sa.column("initial_risk_fraction", sa.Numeric),
        sa.column("maximum_trade_risk_usd", sa.Numeric),
        sa.column("maximum_concurrent_risk_usd", sa.Numeric),
        sa.column("daily_loss_stop_usd", sa.Numeric),
        sa.column("account_floor_usd", sa.Numeric),
        sa.column("reason", sa.Text),
        sa.column("created_by", sa.String),
        sa.column("created_at", sa.DateTime),
    )
    op.bulk_insert(
        revisions,
        [
            {
                "risk_revision_id": "c9314a2f-5e67-58cc-916e-851af737f554",
                "virtual_account_id": MAIN_ACCOUNT_ID,
                "revision_number": 1,
                "initial_risk_fraction": 0.0025,
                "maximum_trade_risk_usd": 130,
                "maximum_concurrent_risk_usd": 780,
                "daily_loss_stop_usd": 520,
                "account_floor_usd": 40000,
                "reason": "Initial governed virtual-account risk policy",
                "created_by": "system-bootstrap",
                "created_at": now,
            }
        ],
    )
    with op.batch_alter_table("shadow_deployments") as batch_op:
        batch_op.add_column(
            sa.Column(
                "virtual_account_id",
                sa.String(length=36),
                nullable=False,
                server_default=MAIN_ACCOUNT_ID,
            )
        )
        batch_op.add_column(sa.Column("strategy_sleeve_id", sa.String(length=36)))
        batch_op.create_index(
            "ix_shadow_deployments_virtual_account_id", ["virtual_account_id"]
        )
        batch_op.create_index(
            "ix_shadow_deployments_strategy_sleeve_id", ["strategy_sleeve_id"]
        )
        batch_op.create_foreign_key(
            "fk_shadow_deployments_virtual_account_id_virtual_accounts",
            "virtual_accounts",
            ["virtual_account_id"],
            ["virtual_account_id"],
        )
        batch_op.create_foreign_key(
            "fk_shadow_deployments_strategy_sleeve_id_strategy_sleeves",
            "strategy_sleeves",
            ["strategy_sleeve_id"],
            ["strategy_sleeve_id"],
        )
    with op.batch_alter_table("shadow_trade_plans") as batch_op:
        batch_op.add_column(
            sa.Column(
                "reserved_cash", sa.Numeric(24, 8), nullable=False, server_default="0"
            )
        )
        batch_op.add_column(
            sa.Column(
                "reserved_risk_usd",
                sa.Numeric(24, 8),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("shadow_trade_plans") as batch_op:
        batch_op.drop_column("reserved_risk_usd")
        batch_op.drop_column("reserved_cash")
    with op.batch_alter_table("shadow_deployments") as batch_op:
        batch_op.drop_constraint(
            "fk_shadow_deployments_strategy_sleeve_id_strategy_sleeves",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_shadow_deployments_virtual_account_id_virtual_accounts",
            type_="foreignkey",
        )
        batch_op.drop_index("ix_shadow_deployments_strategy_sleeve_id")
        batch_op.drop_index("ix_shadow_deployments_virtual_account_id")
        batch_op.drop_column("strategy_sleeve_id")
        batch_op.drop_column("virtual_account_id")
    op.drop_table("strategy_sleeves")
    op.drop_table("virtual_account_risk_revisions")
    op.drop_table("virtual_accounts")
