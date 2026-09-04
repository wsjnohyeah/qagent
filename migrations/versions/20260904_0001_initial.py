"""Create Phase 0 ledger and Phase 1 market-data tables."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ledger_events",
        sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("producer", sa.String(length=80), nullable=False),
        sa.Column("correlation_id", sa.String(length=36), nullable=False),
        sa.Column("causation_id", sa.String(length=36), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("sequence", name="pk_ledger_events"),
        sa.UniqueConstraint("event_id", name="uq_ledger_events_event_id"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_ledger_events_event_type",
        "ledger_events",
        ["event_type"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_ledger_events_correlation_id",
        "ledger_events",
        ["correlation_id"],
        if_not_exists=True,
    )
    op.create_table(
        "raw_objects",
        sa.Column("raw_object_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("data_type", sa.String(length=80), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("payload_bytes", sa.BigInteger(), nullable=False),
        sa.Column("provider_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_metadata", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("raw_object_id", name="pk_raw_objects"),
        sa.UniqueConstraint("provider", "content_sha256", name="uq_raw_objects_provider_sha256"),
        if_not_exists=True,
    )
    op.create_table(
        "market_bars",
        sa.Column("bar_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("high", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("low", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("close", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("trade_count", sa.BigInteger(), nullable=True),
        sa.Column("vwap", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("feed", sa.String(length=20), nullable=False),
        sa.Column("raw_object_id", sa.String(length=36), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("bar_id", name="pk_market_bars"),
        sa.UniqueConstraint(
            "symbol",
            "timeframe",
            "event_time",
            "source",
            "feed",
            name="uq_market_bars_identity",
        ),
        if_not_exists=True,
    )
    op.create_index("ix_market_bars_symbol", "market_bars", ["symbol"], if_not_exists=True)
    op.create_index(
        "ix_market_bars_event_time",
        "market_bars",
        ["event_time"],
        if_not_exists=True,
    )
    op.create_table(
        "ingestion_runs",
        sa.Column("ingestion_run_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("data_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_metadata", sa.JSON(), nullable=False),
        sa.Column("pages_received", sa.Integer(), nullable=False),
        sa.Column("records_received", sa.Integer(), nullable=False),
        sa.Column("records_inserted", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.PrimaryKeyConstraint("ingestion_run_id", name="pk_ingestion_runs"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("ingestion_runs")
    op.drop_index("ix_market_bars_event_time", table_name="market_bars")
    op.drop_index("ix_market_bars_symbol", table_name="market_bars")
    op.drop_table("market_bars")
    op.drop_table("raw_objects")
    op.drop_index("ix_ledger_events_correlation_id", table_name="ledger_events")
    op.drop_index("ix_ledger_events_event_type", table_name="ledger_events")
    op.drop_table("ledger_events")
