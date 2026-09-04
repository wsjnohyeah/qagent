"""Add immutable LLM invocation audit records."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0011"
down_revision: str | None = "20260904_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_invocations",
        sa.Column("invocation_id", sa.String(length=36), nullable=False),
        sa.Column("workload", sa.String(length=80), nullable=False),
        sa.Column("routing_version", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("reasoning_effort", sa.String(length=24), nullable=False),
        sa.Column("prompt_version", sa.String(length=120), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("response_id", sa.String(length=160), nullable=True),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column("output_sha256", sa.String(length=64), nullable=True),
        sa.Column("usage_json", sa.JSON(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("invocation_id", name="pk_llm_invocations"),
    )
    op.create_index("ix_llm_invocations_workload", "llm_invocations", ["workload"])
    op.create_index("ix_llm_invocations_provider", "llm_invocations", ["provider"])
    op.create_index("ix_llm_invocations_model", "llm_invocations", ["model"])
    op.create_index(
        "ix_llm_invocations_request_sha256",
        "llm_invocations",
        ["request_sha256"],
    )
    op.create_index("ix_llm_invocations_status", "llm_invocations", ["status"])
    op.create_index("ix_llm_invocations_created_at", "llm_invocations", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_invocations_created_at", table_name="llm_invocations")
    op.drop_index("ix_llm_invocations_status", table_name="llm_invocations")
    op.drop_index("ix_llm_invocations_request_sha256", table_name="llm_invocations")
    op.drop_index("ix_llm_invocations_model", table_name="llm_invocations")
    op.drop_index("ix_llm_invocations_provider", table_name="llm_invocations")
    op.drop_index("ix_llm_invocations_workload", table_name="llm_invocations")
    op.drop_table("llm_invocations")
