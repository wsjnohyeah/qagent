from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from agentic_quant.domain import EventEnvelope


metadata = MetaData()
ledger_events = Table(
    "ledger_events",
    metadata,
    Column("sequence", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(36), nullable=False, unique=True),
    Column("event_type", String(120), nullable=False, index=True),
    Column("event_time", DateTime(timezone=True), nullable=False),
    Column("emitted_at", DateTime(timezone=True), nullable=False),
    Column("producer", String(80), nullable=False),
    Column("correlation_id", String(36), nullable=False, index=True),
    Column("causation_id", String(36), nullable=True),
    Column("schema_version", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)


class EventLedger:
    def __init__(self, database_url: str) -> None:
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(
            database_url,
            pool_pre_ping=True,
            connect_args=connect_args,
        )

    def initialize(self) -> None:
        metadata.create_all(self.engine)

    def health(self) -> bool:
        with self.engine.connect() as connection:
            return bool(connection.execute(text("SELECT 1")).scalar_one() == 1)

    def append(self, event: EventEnvelope) -> bool:
        record = event.model_dump()
        try:
            with self.engine.begin() as connection:
                connection.execute(insert(ledger_events).values(**record))
        except IntegrityError:
            return False
        return True

    def by_correlation_id(self, correlation_id: str) -> list[dict[str, Any]]:
        statement = (
            select(ledger_events)
            .where(ledger_events.c.correlation_id == correlation_id)
            .order_by(ledger_events.c.sequence)
        )
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        statement = select(ledger_events).order_by(ledger_events.c.sequence.desc()).limit(limit)
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]
