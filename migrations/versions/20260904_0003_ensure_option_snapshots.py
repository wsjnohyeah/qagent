"""Add normalized option snapshots."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0003"
down_revision: str | None = "20260904_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "option_snapshots",
        sa.Column("option_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("contract_symbol", sa.String(length=32), nullable=False),
        sa.Column("underlying_symbol", sa.String(length=24), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bid_price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("bid_size", sa.BigInteger(), nullable=True),
        sa.Column("ask_price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("ask_size", sa.BigInteger(), nullable=True),
        sa.Column("last_trade_price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("last_trade_size", sa.BigInteger(), nullable=True),
        sa.Column("implied_volatility", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("delta", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("gamma", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("theta", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("vega", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("rho", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("feed", sa.String(length=20), nullable=False),
        sa.Column("raw_object_id", sa.String(length=36), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("option_snapshot_id", name="pk_option_snapshots"),
        sa.UniqueConstraint(
            "source",
            "feed",
            "contract_symbol",
            "as_of",
            name="uq_option_snapshots_identity",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_option_snapshots_contract_symbol",
        "option_snapshots",
        ["contract_symbol"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_option_snapshots_underlying_symbol",
        "option_snapshots",
        ["underlying_symbol"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_option_snapshots_as_of",
        "option_snapshots",
        ["as_of"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_option_snapshots_as_of", table_name="option_snapshots")
    op.drop_index("ix_option_snapshots_underlying_symbol", table_name="option_snapshots")
    op.drop_index("ix_option_snapshots_contract_symbol", table_name="option_snapshots")
    op.drop_table("option_snapshots")
