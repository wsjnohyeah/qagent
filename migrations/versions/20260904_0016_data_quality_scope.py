"""Include assessment scope in data-quality report identity."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0016"
down_revision: str | None = "20260904_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT = "uq_data_quality_reports_dataset_hash"
_LEGACY_COLUMNS = (
    "ruleset_version",
    "dataset_type",
    "symbol",
    "timeframe",
    "data_sha256",
)


def upgrade() -> None:
    op.add_column(
        "data_quality_reports",
        sa.Column(
            "scope_sha256",
            sa.String(length=64),
            nullable=False,
            server_default="0" * 64,
        ),
    )
    with op.batch_alter_table("data_quality_reports") as batch:
        batch.drop_constraint(_CONSTRAINT, type_="unique")
        batch.create_unique_constraint(
            _CONSTRAINT,
            (*_LEGACY_COLUMNS, "scope_sha256"),
        )

    with op.batch_alter_table("data_quality_reports") as batch:
        batch.alter_column("scope_sha256", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("data_quality_reports") as batch:
        batch.drop_constraint(_CONSTRAINT, type_="unique")
        batch.create_unique_constraint(_CONSTRAINT, _LEGACY_COLUMNS)
        batch.drop_column("scope_sha256")
