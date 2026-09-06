from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentic_quant.archive import RawArchiveResult
from agentic_quant.database import (
    ingestion_runs,
    market_bars,
    market_quotes,
    market_trades,
    option_snapshots,
    raw_objects,
)
from agentic_quant.domain import OptionSnapshot, StockBar, StockQuote, StockTrade


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class MarketDataStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def start_run(
        self,
        *,
        ingestion_run_id: str,
        provider: str,
        data_type: str,
        requested_at: datetime,
        request_metadata: dict[str, Any],
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(ingestion_runs).values(
                    ingestion_run_id=ingestion_run_id,
                    provider=provider,
                    data_type=data_type,
                    status="RUNNING",
                    requested_at=requested_at,
                    completed_at=None,
                    request_metadata=request_metadata,
                    pages_received=0,
                    records_received=0,
                    records_inserted=0,
                    error_code=None,
                )
            )

    def finish_run(
        self,
        *,
        ingestion_run_id: str,
        status: str,
        completed_at: datetime,
        pages_received: int,
        records_received: int,
        records_inserted: int,
        error_code: str | None = None,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(ingestion_runs)
                .where(ingestion_runs.c.ingestion_run_id == ingestion_run_id)
                .values(
                    status=status,
                    completed_at=completed_at,
                    pages_received=pages_received,
                    records_received=records_received,
                    records_inserted=records_inserted,
                    error_code=error_code,
                )
            )

    def register_raw_object(self, item: RawArchiveResult) -> str:
        identity = (raw_objects.c.provider == item.provider) & (
            raw_objects.c.content_sha256 == item.content_sha256
        )
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(raw_objects.c.raw_object_id).where(identity)
            ).scalar_one_or_none()
            if existing is not None:
                return str(existing)
            if self.engine.dialect.name == "postgresql":
                statement = (
                    postgresql_insert(raw_objects)
                    .values(**item.model_dump())
                    .on_conflict_do_nothing(
                        index_elements=["provider", "content_sha256"]
                    )
                    .returning(raw_objects.c.raw_object_id)
                )
            elif self.engine.dialect.name == "sqlite":
                statement = (
                    sqlite_insert(raw_objects)
                    .values(**item.model_dump())
                    .on_conflict_do_nothing(
                        index_elements=["provider", "content_sha256"]
                    )
                    .returning(raw_objects.c.raw_object_id)
                )
            else:
                raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")
            inserted = connection.execute(statement).scalar_one_or_none()
            if inserted is not None:
                return str(inserted)
            existing = connection.execute(
                select(raw_objects.c.raw_object_id).where(identity)
            ).scalar_one()
            return str(existing)

    def insert_bars(self, bars: tuple[StockBar, ...], raw_object_id: str) -> tuple[str, ...]:
        if not bars:
            return ()
        records = [
            bar.model_copy(update={"raw_object_id": raw_object_id}).model_dump() for bar in bars
        ]
        identity_columns = [
            "symbol",
            "timeframe",
            "event_time",
            "source",
            "feed",
        ]
        with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                statement = (
                    postgresql_insert(market_bars)
                    .values(records)
                    .on_conflict_do_nothing(index_elements=identity_columns)
                    .returning(market_bars.c.bar_id)
                )
            elif self.engine.dialect.name == "sqlite":
                statement = (
                    sqlite_insert(market_bars)
                    .values(records)
                    .on_conflict_do_nothing(index_elements=identity_columns)
                    .returning(market_bars.c.bar_id)
                )
            else:
                raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")
            return tuple(str(value) for value in connection.execute(statement).scalars().all())

    def canonical_bars(self, bars: tuple[StockBar, ...]) -> tuple[StockBar, ...]:
        """Resolve provider rows to the IDs actually persisted under natural keys."""
        values: list[StockBar] = []
        with self.engine.connect() as connection:
            for bar in bars:
                row = connection.execute(
                    select(market_bars).where(
                        (market_bars.c.symbol == bar.symbol)
                        & (market_bars.c.timeframe == bar.timeframe)
                        & (market_bars.c.event_time == bar.event_time)
                        & (market_bars.c.source == bar.source)
                        & (market_bars.c.feed == bar.feed)
                    )
                ).one()
                item = dict(row._mapping)
                for field in ("event_time", "available_from", "ingested_at"):
                    item[field] = _utc(item[field])
                values.append(StockBar.model_validate(item))
        return tuple(values)

    def bars_between(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        source: str,
        feed: str,
    ) -> tuple[StockBar, ...]:
        statement = (
            select(market_bars)
            .where(
                and_(
                    market_bars.c.symbol == symbol.upper(),
                    market_bars.c.timeframe == timeframe,
                    market_bars.c.event_time >= start,
                    market_bars.c.event_time < end,
                    market_bars.c.source == source,
                    market_bars.c.feed == feed,
                )
            )
            .order_by(market_bars.c.event_time.asc())
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        return tuple(
            StockBar.model_validate(
                {
                    **dict(row._mapping),
                    "event_time": _utc(row._mapping["event_time"]),
                    "available_from": _utc(row._mapping["available_from"]),
                    "ingested_at": _utc(row._mapping["ingested_at"]),
                }
            )
            for row in rows
        )

    def insert_trades(
        self, trades: tuple[StockTrade, ...], raw_object_id: str
    ) -> tuple[str, ...]:
        records = [
            trade.model_copy(update={"raw_object_id": raw_object_id}).model_dump()
            for trade in trades
        ]
        return self._insert_market_records(
            table=market_trades,
            records=records,
            identity_columns=["source", "feed", "symbol", "provider_trade_id"],
            returning_column=market_trades.c.trade_id,
        )

    def insert_quotes(
        self, quotes: tuple[StockQuote, ...], raw_object_id: str
    ) -> tuple[str, ...]:
        records = [
            quote.model_copy(update={"raw_object_id": raw_object_id}).model_dump()
            for quote in quotes
        ]
        return self._insert_market_records(
            table=market_quotes,
            records=records,
            identity_columns=["source", "feed", "quote_fingerprint"],
            returning_column=market_quotes.c.quote_id,
        )

    def canonical_trades(
        self,
        trades: tuple[StockTrade, ...],
    ) -> tuple[StockTrade, ...]:
        values: list[StockTrade] = []
        with self.engine.connect() as connection:
            for trade in trades:
                row = connection.execute(
                    select(market_trades).where(
                        (market_trades.c.source == trade.source)
                        & (market_trades.c.feed == trade.feed)
                        & (market_trades.c.symbol == trade.symbol)
                        & (
                            market_trades.c.provider_trade_id
                            == trade.provider_trade_id
                        )
                    )
                ).one()
                item = dict(row._mapping)
                for field in ("event_time", "available_from", "ingested_at"):
                    item[field] = _utc(item[field])
                values.append(StockTrade.model_validate(item))
        return tuple(values)

    def canonical_quotes(
        self,
        quotes: tuple[StockQuote, ...],
    ) -> tuple[StockQuote, ...]:
        values: list[StockQuote] = []
        with self.engine.connect() as connection:
            for quote in quotes:
                row = connection.execute(
                    select(market_quotes).where(
                        (market_quotes.c.source == quote.source)
                        & (market_quotes.c.feed == quote.feed)
                        & (
                            market_quotes.c.quote_fingerprint
                            == quote.quote_fingerprint
                        )
                    )
                ).one()
                item = dict(row._mapping)
                for field in ("event_time", "available_from", "ingested_at"):
                    item[field] = _utc(item[field])
                values.append(StockQuote.model_validate(item))
        return tuple(values)

    def insert_option_snapshots(
        self, snapshots: tuple[OptionSnapshot, ...], raw_object_id: str
    ) -> tuple[str, ...]:
        records = [
            snapshot.model_copy(update={"raw_object_id": raw_object_id}).model_dump()
            for snapshot in snapshots
        ]
        return self._insert_market_records(
            table=option_snapshots,
            records=records,
            identity_columns=["source", "feed", "contract_symbol", "as_of"],
            returning_column=option_snapshots.c.option_snapshot_id,
        )

    def canonical_option_snapshots(
        self,
        snapshots: tuple[OptionSnapshot, ...],
    ) -> tuple[OptionSnapshot, ...]:
        values: list[OptionSnapshot] = []
        with self.engine.connect() as connection:
            for snapshot in snapshots:
                row = connection.execute(
                    select(option_snapshots).where(
                        (option_snapshots.c.source == snapshot.source)
                        & (option_snapshots.c.feed == snapshot.feed)
                        & (
                            option_snapshots.c.contract_symbol
                            == snapshot.contract_symbol
                        )
                        & (option_snapshots.c.as_of == snapshot.as_of)
                    )
                ).one()
                item = dict(row._mapping)
                for field in ("as_of", "available_from", "ingested_at"):
                    item[field] = _utc(item[field])
                values.append(OptionSnapshot.model_validate(item))
        return tuple(values)

    def _insert_market_records(
        self,
        *,
        table: Any,
        records: list[dict[str, Any]],
        identity_columns: list[str],
        returning_column: Any,
    ) -> tuple[str, ...]:
        if not records:
            return ()
        with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                statement = (
                    postgresql_insert(table)
                    .values(records)
                    .on_conflict_do_nothing(index_elements=identity_columns)
                    .returning(returning_column)
                )
            elif self.engine.dialect.name == "sqlite":
                statement = (
                    sqlite_insert(table)
                    .values(records)
                    .on_conflict_do_nothing(index_elements=identity_columns)
                    .returning(returning_column)
                )
            else:
                raise RuntimeError(f"Unsupported SQL dialect: {self.engine.dialect.name}")
            return tuple(str(value) for value in connection.execute(statement).scalars().all())

    def health_summary(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            bar_count = connection.execute(
                select(func.count()).select_from(market_bars)
            ).scalar_one()
            raw_count = connection.execute(
                select(func.count()).select_from(raw_objects)
            ).scalar_one()
            run_count = connection.execute(
                select(func.count()).select_from(ingestion_runs)
            ).scalar_one()
            trade_count = connection.execute(
                select(func.count()).select_from(market_trades)
            ).scalar_one()
            quote_count = connection.execute(
                select(func.count()).select_from(market_quotes)
            ).scalar_one()
            option_count = connection.execute(
                select(func.count()).select_from(option_snapshots)
            ).scalar_one()
            last_event = connection.execute(select(func.max(market_bars.c.event_time))).scalar_one()
        return {
            "market_bars": int(bar_count),
            "raw_objects": int(raw_count),
            "ingestion_runs": int(run_count),
            "market_trades": int(trade_count),
            "market_quotes": int(quote_count),
            "option_snapshots": int(option_count),
            "latest_bar_event_time": last_event,
        }

    def latest_bar_event_time(
        self,
        *,
        symbol: str,
        timeframe: str,
        source: str,
        feed: str,
    ) -> datetime | None:
        statement = select(func.max(market_bars.c.event_time)).where(
            (market_bars.c.symbol == symbol.upper())
            & (market_bars.c.timeframe == timeframe)
            & (market_bars.c.source == source)
            & (market_bars.c.feed == feed)
        )
        with self.engine.connect() as connection:
            value = connection.execute(statement).scalar_one()
        if not isinstance(value, datetime):
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
