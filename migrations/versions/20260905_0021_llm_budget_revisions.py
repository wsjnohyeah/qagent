"""Add immutable Control Center LLM budget revisions."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260905_0021"
down_revision: str | None = "20260905_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_budget_revisions",
        sa.Column("budget_revision_id", sa.String(length=36), nullable=False),
        sa.Column("base_policy_version", sa.String(length=80), nullable=False),
        sa.Column("base_policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=120), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("workload_limits_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "budget_revision_id",
            name="pk_llm_budget_revisions",
        ),
        sa.UniqueConstraint(
            "policy_version",
            name="uq_llm_budget_revisions_policy_version",
        ),
    )
    op.create_index(
        "ix_llm_budget_revisions_base_policy_sha256",
        "llm_budget_revisions",
        ["base_policy_sha256"],
    )
    op.create_index(
        "ix_llm_budget_revisions_created_at",
        "llm_budget_revisions",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_llm_budget_revisions_created_at",
        table_name="llm_budget_revisions",
    )
    op.drop_index(
        "ix_llm_budget_revisions_base_policy_sha256",
        table_name="llm_budget_revisions",
    )
    op.drop_table("llm_budget_revisions")
