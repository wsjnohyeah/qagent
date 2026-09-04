"""Add normalized live stock trades and quotes."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0002"
down_revision: str | None = "20260904_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_trades",
        sa.Column("trade_id", sa.String(length=36), nullable=False),
        sa.Column("provider_trade_id", sa.String(length=80), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("tape", sa.String(length=8), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("feed", sa.String(length=20), nullable=False),
        sa.Column("raw_object_id", sa.String(length=36), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("trade_id", name="pk_market_trades"),
        sa.UniqueConstraint(
            "source",
            "feed",
            "symbol",
            "provider_trade_id",
            name="uq_market_trades_provider_identity",
        ),
    )
    op.create_index("ix_market_trades_symbol", "market_trades", ["symbol"])
    op.create_index("ix_market_trades_event_time", "market_trades", ["event_time"])
    op.create_table(
        "market_quotes",
        sa.Column("quote_id", sa.String(length=36), nullable=False),
        sa.Column("quote_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bid_exchange", sa.String(length=20), nullable=False),
        sa.Column("bid_price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("bid_size", sa.BigInteger(), nullable=False),
        sa.Column("ask_exchange", sa.String(length=20), nullable=False),
        sa.Column("ask_price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("ask_size", sa.BigInteger(), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("tape", sa.String(length=8), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("feed", sa.String(length=20), nullable=False),
        sa.Column("raw_object_id", sa.String(length=36), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("quote_id", name="pk_market_quotes"),
        sa.UniqueConstraint(
            "source",
            "feed",
            "quote_fingerprint",
            name="uq_market_quotes_provider_identity",
        ),
    )
    op.create_index("ix_market_quotes_symbol", "market_quotes", ["symbol"])
    op.create_index("ix_market_quotes_event_time", "market_quotes", ["event_time"])


def downgrade() -> None:
    op.drop_index("ix_market_quotes_event_time", table_name="market_quotes")
    op.drop_index("ix_market_quotes_symbol", table_name="market_quotes")
    op.drop_table("market_quotes")
    op.drop_index("ix_market_trades_event_time", table_name="market_trades")
    op.drop_index("ix_market_trades_symbol", table_name="market_trades")
    op.drop_table("market_trades")
