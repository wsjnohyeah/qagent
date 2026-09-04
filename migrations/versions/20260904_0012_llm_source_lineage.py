"""Add source and routing hashes to LLM invocation lineage."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0012"
down_revision: str | None = "20260904_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("llm_invocations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "routing_sha256",
                sa.String(length=64),
                nullable=False,
                server_default="UNKNOWN",
            )
        )
        batch_op.add_column(
            sa.Column(
                "code_git_sha",
                sa.String(length=64),
                nullable=False,
                server_default="UNAVAILABLE",
            )
        )
    with op.batch_alter_table("llm_invocations") as batch_op:
        batch_op.alter_column("routing_sha256", server_default=None)
        batch_op.alter_column("code_git_sha", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("llm_invocations") as batch_op:
        batch_op.drop_column("code_git_sha")
        batch_op.drop_column("routing_sha256")
