from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from agentic_quant.domain import EventEnvelope
from agentic_quant.database import ledger_events, metadata


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
