"""Normalize a legacy PostgreSQL ledger constraint name."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect


revision: str = "20260905_0020"
down_revision: str | None = "20260905_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _constraint_names() -> set[str | None]:
    return {
        item.get("name")
        for item in inspect(op.get_bind()).get_unique_constraints("ledger_events")
    }


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    names = _constraint_names()
    if (
        "ledger_events_event_id_key" in names
        and "uq_ledger_events_event_id" not in names
    ):
        op.execute(
            "ALTER TABLE ledger_events RENAME CONSTRAINT "
            "ledger_events_event_id_key TO uq_ledger_events_event_id"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    names = _constraint_names()
    if (
        "uq_ledger_events_event_id" in names
        and "ledger_events_event_id_key" not in names
    ):
        op.execute(
            "ALTER TABLE ledger_events RENAME CONSTRAINT "
            "uq_ledger_events_event_id TO ledger_events_event_id_key"
        )
