from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Engine, select

from agentic_quant.database import ledger_events


MARKET_HISTORY_BOUNDARY_EVENT = "market.history.boundary.observed.v1"
MARKET_HISTORY_BOUNDARY_POLICY_VERSION = "market_history_boundary@0.2.0"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Market-history boundary timestamps must be timezone-aware")
    return value.astimezone(UTC)


def known_market_history_boundary(
    engine: Engine,
    *,
    symbol: str,
    timeframe: str,
    desired_start: datetime,
    known_at: datetime,
    source: str,
    feed: str,
) -> tuple[str, datetime] | None:
    """Return the newest applicable, point-in-time history boundary.

    A boundary is reusable only when its provider probe covered the requested leading
    window and the boundary event was already known at the caller's observation time.
    """
    desired_start_utc = _utc(desired_start)
    known_at_utc = _utc(known_at)
    with engine.connect() as connection:
        rows = connection.execute(
            select(ledger_events.c.event_id, ledger_events.c.payload)
            .where(
                ledger_events.c.event_type == MARKET_HISTORY_BOUNDARY_EVENT,
                ledger_events.c.event_time <= known_at_utc,
                ledger_events.c.payload["symbol"].as_string() == symbol.upper(),
                ledger_events.c.payload["timeframe"].as_string() == timeframe,
            )
            .order_by(ledger_events.c.sequence.desc())
            .limit(100)
        ).all()
    for row in rows:
        payload = dict(row.payload)
        if (
            payload.get("source") != source
            or payload.get("feed") != feed
            or payload.get("policy_version")
            != MARKET_HISTORY_BOUNDARY_POLICY_VERSION
        ):
            continue
        probed_start = _utc(datetime.fromisoformat(str(payload["probed_start"])))
        observed_start = _utc(
            datetime.fromisoformat(str(payload["observed_start"]))
        )
        if probed_start <= desired_start_utc:
            return str(row.event_id), max(desired_start_utc, observed_start)
    return None
