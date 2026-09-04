"""Add corporate actions, universe membership, and feature parity checks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from alembic import op
import exchange_calendars as exchange_calendars  # type: ignore[import-untyped]
import sqlalchemy as sa


revision: str = "20260904_0008"
down_revision: str | None = "20260904_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rewrite_daily_bar_availability(*, exact_session_close: bool) -> None:
    market_bars = sa.table(
        "market_bars",
        sa.column("bar_id", sa.String(length=36)),
        sa.column("timeframe", sa.String(length=16)),
        sa.column("event_time", sa.DateTime(timezone=True)),
        sa.column("available_from", sa.DateTime(timezone=True)),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(market_bars.c.bar_id, market_bars.c.event_time).where(
            market_bars.c.timeframe == "1Day"
        )
    ).all()
    if not rows:
        return
    calendar = exchange_calendars.get_calendar(
        "XNYS",
        start=min(row.event_time.date() for row in rows) - timedelta(days=1),
        end=max(row.event_time.date() for row in rows) + timedelta(days=1),
    )
    for row in rows:
        if exact_session_close:
            session_label = row.event_time.date().isoformat()
            try:
                session = calendar.date_to_session(session_label, direction="none")
            except ValueError as exc:
                raise RuntimeError(
                    f"Cannot migrate daily bar {row.bar_id}: "
                    f"{session_label} is not an XNYS session"
                ) from exc
            available_from = calendar.session_close(session).to_pydatetime()
        else:
            available_from = row.event_time + timedelta(days=1)
        connection.execute(
            market_bars.update()
            .where(market_bars.c.bar_id == row.bar_id)
            .values(available_from=available_from)
        )


def upgrade() -> None:
    _rewrite_daily_bar_availability(exact_session_close=True)
    op.create_table(
        "corporate_actions",
        sa.Column("corporate_action_id", sa.String(length=36), nullable=False),
        sa.Column("action_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("split_ratio", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("cash_amount", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("new_symbol", sa.String(length=24), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("raw_object_id", sa.String(length=36), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("corporate_action_id", name="pk_corporate_actions"),
        sa.UniqueConstraint(
            "action_fingerprint",
            name="uq_corporate_actions_action_fingerprint",
        ),
    )
    op.create_index("ix_corporate_actions_symbol", "corporate_actions", ["symbol"])
    op.create_index(
        "ix_corporate_actions_action_type",
        "corporate_actions",
        ["action_type"],
    )
    op.create_index(
        "ix_corporate_actions_effective_at",
        "corporate_actions",
        ["effective_at"],
    )
    op.create_index(
        "ix_corporate_actions_available_from",
        "corporate_actions",
        ["available_from"],
    )

    op.create_table(
        "universe_memberships",
        sa.Column("membership_id", sa.String(length=36), nullable=False),
        sa.Column("universe", sa.String(length=80), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("source_version", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("membership_id", name="pk_universe_memberships"),
        sa.UniqueConstraint(
            "universe",
            "symbol",
            "effective_from",
            "source",
            "source_version",
            name="uq_universe_memberships_identity",
        ),
    )
    op.create_index(
        "ix_universe_memberships_universe",
        "universe_memberships",
        ["universe"],
    )
    op.create_index(
        "ix_universe_memberships_symbol",
        "universe_memberships",
        ["symbol"],
    )
    op.create_index(
        "ix_universe_memberships_effective_from",
        "universe_memberships",
        ["effective_from"],
    )

    op.create_table(
        "feature_parity_checks",
        sa.Column("parity_check_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("feature_set_version", sa.String(length=80), nullable=False),
        sa.Column("offline_data_hash", sa.String(length=64), nullable=False),
        sa.Column("online_data_hash", sa.String(length=64), nullable=False),
        sa.Column("matched", sa.Boolean(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("parity_check_id", name="pk_feature_parity_checks"),
    )
    op.create_index(
        "ix_feature_parity_checks_symbol",
        "feature_parity_checks",
        ["symbol"],
    )
    op.create_index(
        "ix_feature_parity_checks_as_of",
        "feature_parity_checks",
        ["as_of"],
    )


def downgrade() -> None:
    op.drop_index("ix_feature_parity_checks_as_of", table_name="feature_parity_checks")
    op.drop_index("ix_feature_parity_checks_symbol", table_name="feature_parity_checks")
    op.drop_table("feature_parity_checks")
    op.drop_index(
        "ix_universe_memberships_effective_from",
        table_name="universe_memberships",
    )
    op.drop_index("ix_universe_memberships_symbol", table_name="universe_memberships")
    op.drop_index("ix_universe_memberships_universe", table_name="universe_memberships")
    op.drop_table("universe_memberships")
    op.drop_index("ix_corporate_actions_available_from", table_name="corporate_actions")
    op.drop_index("ix_corporate_actions_effective_at", table_name="corporate_actions")
    op.drop_index("ix_corporate_actions_action_type", table_name="corporate_actions")
    op.drop_index("ix_corporate_actions_symbol", table_name="corporate_actions")
    op.drop_table("corporate_actions")
    _rewrite_daily_bar_availability(exact_session_close=False)
