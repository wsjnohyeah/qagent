"""Add LLM budget accounting and evidence-bound research analyses."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0017"
down_revision: str | None = "20260904_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_budget_windows",
        sa.Column("window_key", sa.String(length=180), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("scope", sa.String(length=120), nullable=False),
        sa.Column("period_kind", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_limit", sa.BigInteger(), nullable=False),
        sa.Column("cost_limit_microusd", sa.BigInteger(), nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
        sa.Column("consumed_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reserved_cost_microusd", sa.BigInteger(), nullable=False),
        sa.Column("consumed_cost_microusd", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("window_key", name="pk_llm_budget_windows"),
    )
    op.create_index("ix_llm_budget_windows_scope", "llm_budget_windows", ["scope"])
    op.create_index(
        "ix_llm_budget_windows_period_start",
        "llm_budget_windows",
        ["period_start"],
    )

    op.create_table(
        "llm_budget_reservations",
        sa.Column("invocation_id", sa.String(length=36), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("workload", sa.String(length=80), nullable=False),
        sa.Column("reserved_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reserved_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reserved_cost_microusd", sa.BigInteger(), nullable=False),
        sa.Column("actual_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("actual_output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("actual_cost_microusd", sa.BigInteger(), nullable=True),
        sa.Column("window_keys_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("invocation_id", name="pk_llm_budget_reservations"),
    )
    op.create_index(
        "ix_llm_budget_reservations_provider",
        "llm_budget_reservations",
        ["provider"],
    )
    op.create_index(
        "ix_llm_budget_reservations_workload",
        "llm_budget_reservations",
        ["workload"],
    )
    op.create_index(
        "ix_llm_budget_reservations_status",
        "llm_budget_reservations",
        ["status"],
    )

    op.create_table(
        "research_analyses",
        sa.Column("analysis_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("evidence_bundle_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_bundle_json", sa.JSON(), nullable=False),
        sa.Column("feature_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("forecast_id", sa.String(length=36), nullable=True),
        sa.Column("llm_invocation_id", sa.String(length=36), nullable=True),
        sa.Column("analysis_json", sa.JSON(), nullable=True),
        sa.Column("citation_validation_json", sa.JSON(), nullable=False),
        sa.Column("rejection_reason", sa.String(length=240), nullable=True),
        sa.Column("code_git_sha", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["feature_snapshot_id"],
            ["feature_snapshots.feature_snapshot_id"],
            name="fk_research_analyses_feature_snapshot_id_feature_snapshots",
        ),
        sa.ForeignKeyConstraint(
            ["llm_invocation_id"],
            ["llm_invocations.invocation_id"],
            name="fk_research_analyses_llm_invocation_id_llm_invocations",
        ),
        sa.PrimaryKeyConstraint("analysis_id", name="pk_research_analyses"),
    )
    for column in (
        "symbol",
        "as_of",
        "status",
        "evidence_bundle_hash",
        "feature_snapshot_id",
        "forecast_id",
        "llm_invocation_id",
        "created_at",
    ):
        op.create_index(f"ix_research_analyses_{column}", "research_analyses", [column])


def downgrade() -> None:
    for column in (
        "created_at",
        "llm_invocation_id",
        "forecast_id",
        "feature_snapshot_id",
        "evidence_bundle_hash",
        "status",
        "as_of",
        "symbol",
    ):
        op.drop_index(f"ix_research_analyses_{column}", table_name="research_analyses")
    op.drop_table("research_analyses")
    for column in ("status", "workload", "provider"):
        op.drop_index(
            f"ix_llm_budget_reservations_{column}",
            table_name="llm_budget_reservations",
        )
    op.drop_table("llm_budget_reservations")
    op.drop_index("ix_llm_budget_windows_period_start", table_name="llm_budget_windows")
    op.drop_index("ix_llm_budget_windows_scope", table_name="llm_budget_windows")
    op.drop_table("llm_budget_windows")
