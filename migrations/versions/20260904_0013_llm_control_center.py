"""Add immutable Control Center LLM routing revisions."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0013"
down_revision: str | None = "20260904_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_routing_revisions",
        sa.Column("routing_revision_id", sa.String(length=36), nullable=False),
        sa.Column("base_routing_version", sa.String(length=80), nullable=False),
        sa.Column("base_routing_sha256", sa.String(length=64), nullable=False),
        sa.Column("routing_version", sa.String(length=120), nullable=False),
        sa.Column("routing_sha256", sa.String(length=64), nullable=False),
        sa.Column("routes_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "routing_revision_id",
            name="pk_llm_routing_revisions",
        ),
        sa.UniqueConstraint(
            "routing_version",
            name="uq_llm_routing_revisions_routing_version",
        ),
    )
    op.create_index(
        "ix_llm_routing_revisions_base_routing_sha256",
        "llm_routing_revisions",
        ["base_routing_sha256"],
    )
    op.create_index(
        "ix_llm_routing_revisions_created_at",
        "llm_routing_revisions",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_llm_routing_revisions_created_at",
        table_name="llm_routing_revisions",
    )
    op.drop_index(
        "ix_llm_routing_revisions_base_routing_sha256",
        table_name="llm_routing_revisions",
    )
    op.drop_table("llm_routing_revisions")
