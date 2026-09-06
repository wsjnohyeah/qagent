"""Fence workflow attempts and audit every strategy-generation attempt."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260906_0026"
down_revision: str | None = "20260905_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_jobs") as batch_op:
        batch_op.add_column(sa.Column("lease_token", sa.String(length=36)))
        batch_op.create_index("ix_workflow_jobs_lease_token", ["lease_token"])

    op.create_table(
        "runtime_leases",
        sa.Column("lease_key", sa.String(length=80), nullable=False),
        sa.Column("lease_owner", sa.String(length=120), nullable=False),
        sa.Column("lease_token", sa.String(length=36), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("lease_key"),
        sa.UniqueConstraint("lease_token"),
    )
    op.create_index("ix_runtime_leases_lease_owner", "runtime_leases", ["lease_owner"])
    op.create_index(
        "ix_runtime_leases_lease_expires_at",
        "runtime_leases",
        ["lease_expires_at"],
    )

    op.create_table(
        "strategy_generation_attempts",
        sa.Column("generation_attempt_id", sa.String(length=36), nullable=False),
        sa.Column("feature_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("analysis_id", sa.String(length=36), nullable=False),
        sa.Column("forecast_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("provider", sa.String(length=24)),
        sa.Column("generation_invocation_id", sa.String(length=36)),
        sa.Column("critique_invocation_id", sa.String(length=36)),
        sa.Column("strategy_spec_id", sa.String(length=36)),
        sa.Column("proposal_json", sa.JSON()),
        sa.Column("critique_json", sa.JSON()),
        sa.Column("error_code", sa.String(length=120)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["strategy_spec_id"], ["strategy_specs.strategy_spec_id"]),
        sa.PrimaryKeyConstraint("generation_attempt_id"),
    )
    for column in (
        "feature_snapshot_id",
        "analysis_id",
        "forecast_id",
        "status",
        "strategy_spec_id",
        "created_at",
    ):
        op.create_index(
            f"ix_strategy_generation_attempts_{column}",
            "strategy_generation_attempts",
            [column],
        )


def downgrade() -> None:
    op.drop_table("strategy_generation_attempts")
    op.drop_table("runtime_leases")
    with op.batch_alter_table("workflow_jobs") as batch_op:
        batch_op.drop_index("ix_workflow_jobs_lease_token")
        batch_op.drop_column("lease_token")
