"""Add durable ownership leases and explicit workflow dependencies."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260905_0024"
down_revision: str | None = "20260905_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "dependency_job_ids_json",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )
        batch_op.add_column(sa.Column("lease_owner", sa.String(length=120)))
        batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
        batch_op.create_index(
            "ix_workflow_jobs_lease_owner",
            ["lease_owner"],
        )
        batch_op.create_index(
            "ix_workflow_jobs_lease_expires_at",
            ["lease_expires_at"],
        )


def downgrade() -> None:
    with op.batch_alter_table("workflow_jobs") as batch_op:
        batch_op.drop_index("ix_workflow_jobs_lease_expires_at")
        batch_op.drop_index("ix_workflow_jobs_lease_owner")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_owner")
        batch_op.drop_column("dependency_job_ids_json")
