"""Persist complete broker-free shadow decision lineage."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260905_0023"
down_revision: str | None = "20260905_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shadow_signal_candidates",
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("shadow_deployment_id", sa.String(length=36), nullable=False),
        sa.Column("shadow_run_id", sa.String(length=36), nullable=False),
        sa.Column("strategy_spec_id", sa.String(length=36), nullable=False),
        sa.Column("feature_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("decision_bar_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_entry", sa.Numeric(20, 8), nullable=False),
        sa.Column("invalidation", sa.Numeric(20, 8), nullable=False),
        sa.Column("targets_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execution_contract_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["shadow_deployment_id"],
            ["shadow_deployments.shadow_deployment_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["strategy_spec_id"], ["strategy_specs.strategy_spec_id"]),
        sa.ForeignKeyConstraint(
            ["feature_snapshot_id"], ["feature_snapshots.feature_snapshot_id"]
        ),
        sa.PrimaryKeyConstraint("candidate_id"),
        sa.UniqueConstraint(
            "shadow_deployment_id",
            "decision_bar_id",
            name="uq_shadow_candidate_deployment_bar",
        ),
    )
    for column in (
        "shadow_deployment_id",
        "shadow_run_id",
        "strategy_spec_id",
        "feature_snapshot_id",
        "decision_bar_id",
        "symbol",
    ):
        op.create_index(
            f"ix_shadow_signal_candidates_{column}",
            "shadow_signal_candidates",
            [column],
        )
    op.create_table(
        "shadow_risk_decisions",
        sa.Column("risk_decision_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False),
        sa.Column("account_equity", sa.Numeric(24, 8), nullable=False),
        sa.Column("daily_pnl", sa.Numeric(24, 8), nullable=False),
        sa.Column("concurrent_planned_risk", sa.Numeric(24, 8), nullable=False),
        sa.Column("risk_budget_usd", sa.Numeric(24, 8), nullable=False),
        sa.Column("max_quantity", sa.Integer(), nullable=False),
        sa.Column("planned_entry", sa.Numeric(20, 8), nullable=False),
        sa.Column("invalidation", sa.Numeric(20, 8), nullable=False),
        sa.Column("planned_r_multiple_to_t1", sa.Numeric(20, 8), nullable=True),
        sa.Column("portfolio_risk_after_usd", sa.Numeric(24, 8), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("evaluation_context_json", sa.JSON(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["shadow_signal_candidates.candidate_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("risk_decision_id"),
        sa.UniqueConstraint("candidate_id"),
    )
    op.create_index("ix_shadow_risk_decisions_verdict", "shadow_risk_decisions", ["verdict"])
    op.create_index(
        "ix_shadow_risk_decisions_evaluated_at",
        "shadow_risk_decisions",
        ["evaluated_at"],
    )
    op.create_table(
        "shadow_trade_plans",
        sa.Column("trade_plan_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("risk_decision_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("invalidation", sa.Numeric(20, 8), nullable=False),
        sa.Column("targets_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["shadow_signal_candidates.candidate_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["risk_decision_id"],
            ["shadow_risk_decisions.risk_decision_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("trade_plan_id"),
        sa.UniqueConstraint("candidate_id"),
        sa.UniqueConstraint("risk_decision_id"),
    )
    op.create_index("ix_shadow_trade_plans_symbol", "shadow_trade_plans", ["symbol"])
    op.create_index("ix_shadow_trade_plans_status", "shadow_trade_plans", ["status"])


def downgrade() -> None:
    op.drop_index("ix_shadow_trade_plans_status", table_name="shadow_trade_plans")
    op.drop_index("ix_shadow_trade_plans_symbol", table_name="shadow_trade_plans")
    op.drop_table("shadow_trade_plans")
    op.drop_index("ix_shadow_risk_decisions_evaluated_at", table_name="shadow_risk_decisions")
    op.drop_index("ix_shadow_risk_decisions_verdict", table_name="shadow_risk_decisions")
    op.drop_table("shadow_risk_decisions")
    for column in reversed(
        (
            "shadow_deployment_id",
            "shadow_run_id",
            "strategy_spec_id",
            "feature_snapshot_id",
            "decision_bar_id",
            "symbol",
        )
    ):
        op.drop_index(
            f"ix_shadow_signal_candidates_{column}",
            table_name="shadow_signal_candidates",
        )
    op.drop_table("shadow_signal_candidates")
