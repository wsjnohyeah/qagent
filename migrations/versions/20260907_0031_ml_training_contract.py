"""Bind cached ML training runs to the complete behavior contract."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260907_0031"
down_revision: str | None = "20260906_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LEGACY_UNKNOWN_CONTRACT = "0" * 64


def upgrade() -> None:
    op.execute(
        "UPDATE workflow_jobs SET status = 'EXHAUSTED' "
        "WHERE status = 'FAILED' AND attempt_count >= max_attempts"
    )
    op.add_column(
        "ml_training_runs",
        sa.Column(
            "training_contract_sha256",
            sa.String(length=64),
            nullable=False,
            server_default=_LEGACY_UNKNOWN_CONTRACT,
        ),
    )
    op.create_index(
        "ix_ml_training_runs_training_contract_sha256",
        "ml_training_runs",
        ["training_contract_sha256"],
    )
    with op.batch_alter_table("ml_training_runs") as batch_op:
        batch_op.alter_column(
            "training_contract_sha256",
            existing_type=sa.String(length=64),
            existing_nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    op.drop_index(
        "ix_ml_training_runs_training_contract_sha256",
        table_name="ml_training_runs",
    )
    op.drop_column("ml_training_runs", "training_contract_sha256")
    op.execute("UPDATE workflow_jobs SET status = 'FAILED' WHERE status = 'EXHAUSTED'")
