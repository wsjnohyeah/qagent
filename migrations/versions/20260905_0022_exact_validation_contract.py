"""Bind validation reports to exact executable strategy contracts."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260905_0022"
down_revision: str | None = "20260905_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("validation_reports") as batch_op:
        batch_op.add_column(
            sa.Column(
                "validation_subject",
                sa.String(length=32),
                nullable=False,
                server_default="legacy_unbound",
            )
        )
        batch_op.add_column(
            sa.Column(
                "validated_strategy_spec_ids",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )
        batch_op.add_column(
            sa.Column(
                "execution_contract_json",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )
        batch_op.add_column(
            sa.Column(
                "execution_contract_sha256",
                sa.String(length=64),
                nullable=False,
                server_default="0000000000000000000000000000000000000000000000000000000000000000",
            )
        )
        batch_op.create_index(
            "ix_validation_reports_execution_contract_sha256",
            ["execution_contract_sha256"],
        )
    with op.batch_alter_table("llm_invocations") as batch_op:
        batch_op.add_column(
            sa.Column("request_envelope_json", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("llm_invocations") as batch_op:
        batch_op.drop_column("request_envelope_json")
    with op.batch_alter_table("validation_reports") as batch_op:
        batch_op.drop_index("ix_validation_reports_execution_contract_sha256")
        batch_op.drop_column("execution_contract_sha256")
        batch_op.drop_column("execution_contract_json")
        batch_op.drop_column("validated_strategy_spec_ids")
        batch_op.drop_column("validation_subject")
