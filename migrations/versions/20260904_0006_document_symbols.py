"""Retain exact per-document symbol tags."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0006"
down_revision: str | None = "20260904_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_symbols",
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["source_documents.document_id"],
            name="fk_document_symbols_document_id_source_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("document_id", "symbol", name="pk_document_symbols"),
    )
    op.create_index("ix_document_symbols_symbol", "document_symbols", ["symbol"])
    op.execute(
        "INSERT INTO document_symbols (document_id, symbol) "
        "SELECT de.document_id, e.primary_symbol "
        "FROM document_entities de JOIN entities e ON e.entity_id = de.entity_id"
    )


def downgrade() -> None:
    op.drop_index("ix_document_symbols_symbol", table_name="document_symbols")
    op.drop_table("document_symbols")
