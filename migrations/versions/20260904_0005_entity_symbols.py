"""Add symbol aliases for multi-class issuer entity resolution."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0005"
down_revision: str | None = "20260904_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entity_symbols",
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.entity_id"],
            name="fk_entity_symbols_entity_id_entities",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("entity_id", "symbol", name="pk_entity_symbols"),
    )
    op.create_index("ix_entity_symbols_symbol", "entity_symbols", ["symbol"])
    op.execute(
        "INSERT INTO entity_symbols (entity_id, symbol) "
        "SELECT entity_id, primary_symbol FROM entities"
    )


def downgrade() -> None:
    op.drop_index("ix_entity_symbols_symbol", table_name="entity_symbols")
    op.drop_table("entity_symbols")
