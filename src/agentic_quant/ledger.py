from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, create_engine, func, insert, or_, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from agentic_quant.domain import EventEnvelope
from agentic_quant.database import event_outbox, ledger_events, metadata


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
                connection.execute(
                    insert(event_outbox).values(
                        event_id=event.event_id,
                        event_type=event.event_type,
                        envelope_json=event.model_dump_json(),
                        status="PENDING",
                        attempt_count=0,
                        lease_owner=None,
                        lease_expires_at=None,
                        next_attempt_at=event.emitted_at,
                        last_error=None,
                        created_at=event.emitted_at,
                        updated_at=event.emitted_at,
                        published_at=None,
                    )
                )
        except IntegrityError:
            return False
        return True

    def deliver(self, event: EventEnvelope, publisher: Any) -> str | None:
        """Attempt immediate delivery for an event already committed to the outbox."""
        try:
            receipt = publisher.publish(
                event_type=event.event_type,
                event_id=event.event_id,
                envelope_json=event.model_dump_json(),
            )
        except Exception as exc:
            self._mark_delivery_failure(event.event_id, exc)
            raise
        self._mark_published(event.event_id)
        return str(receipt) if receipt is not None else None

    def publish_pending(
        self,
        publisher: Any,
        *,
        worker_id: str,
        limit: int = 500,
        lease_for: timedelta = timedelta(minutes=2),
        maximum_attempts: int = 8,
    ) -> dict[str, int]:
        """Deliver outbox rows at least once; event_id is the consumer dedup key."""
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        now = datetime.now(UTC)
        eligible = or_(
            event_outbox.c.status.in_(("PENDING", "FAILED")),
            and_(
                event_outbox.c.status == "PUBLISHING",
                event_outbox.c.lease_expires_at <= now,
            ),
        )
        with self.engine.connect() as connection:
            event_ids = list(
                connection.execute(
                    select(event_outbox.c.event_id)
                    .where(
                        eligible,
                        event_outbox.c.next_attempt_at <= now,
                        event_outbox.c.attempt_count < maximum_attempts,
                    )
                    .order_by(event_outbox.c.created_at.asc())
                    .limit(limit)
                ).scalars()
            )
        published = failed = 0
        for event_id in event_ids:
            with self.engine.begin() as connection:
                claimed = connection.execute(
                    update(event_outbox)
                    .where(
                        event_outbox.c.event_id == event_id,
                        eligible,
                        event_outbox.c.next_attempt_at <= now,
                        event_outbox.c.attempt_count < maximum_attempts,
                    )
                    .values(
                        status="PUBLISHING",
                        lease_owner=worker_id,
                        lease_expires_at=now + lease_for,
                        attempt_count=event_outbox.c.attempt_count + 1,
                        updated_at=now,
                    )
                )
                if int(claimed.rowcount or 0) != 1:
                    continue
                row = connection.execute(
                    select(event_outbox).where(event_outbox.c.event_id == event_id)
                ).one()
            try:
                publisher.publish(
                    event_type=str(row.event_type),
                    event_id=str(row.event_id),
                    envelope_json=str(row.envelope_json),
                )
            except Exception as exc:
                failed += 1
                self._mark_delivery_failure(str(event_id), exc)
            else:
                published += 1
                self._mark_published(str(event_id))
        with self.engine.begin() as connection:
            connection.execute(
                update(event_outbox)
                .where(event_outbox.c.attempt_count >= maximum_attempts)
                .where(event_outbox.c.status == "FAILED")
                .values(status="DEAD", updated_at=datetime.now(UTC))
            )
        return {"published": published, "failed": failed}

    def outbox_health(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            counts = {
                str(status).casefold(): int(count)
                for status, count in connection.execute(
                    select(event_outbox.c.status, func.count()).group_by(
                        event_outbox.c.status
                    )
                )
            }
        return {
            f"event_outbox_{status}": counts.get(status, 0)
            for status in ("pending", "publishing", "failed", "dead", "published")
        }

    def _mark_published(self, event_id: str) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            connection.execute(
                update(event_outbox)
                .where(event_outbox.c.event_id == event_id)
                .values(
                    status="PUBLISHED",
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error=None,
                    updated_at=now,
                    published_at=now,
                )
            )

    def _mark_delivery_failure(self, event_id: str, error: Exception) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(event_outbox.c.attempt_count).where(
                    event_outbox.c.event_id == event_id
                )
            ).scalar_one_or_none()
            if row is None:
                return
            attempt_count = int(row)
            retry_delay = min(300, 2 ** min(attempt_count, 8))
            connection.execute(
                update(event_outbox)
                .where(event_outbox.c.event_id == event_id)
                .values(
                    status="FAILED",
                    lease_owner=None,
                    lease_expires_at=None,
                    next_attempt_at=now + timedelta(seconds=retry_delay),
                    last_error=type(error).__name__[:240],
                    updated_at=now,
                )
            )

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
