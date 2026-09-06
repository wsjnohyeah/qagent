from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from agentic_quant.archive import RawArchive
from agentic_quant.domain import EventEnvelope, StockBar, StockQuote, StockTrade
from agentic_quant.ledger import EventLedger
from agentic_quant.market_calendar import MarketDataGap, MarketGapDetector
from agentic_quant.market_store import MarketDataStore
from agentic_quant.providers.alpaca_stream import normalize_stream_message
from agentic_quant.providers.base import EventPublisher


class LiveFrameSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages_received: int
    records_inserted: int
    ignored_messages: int
    gaps: tuple[MarketDataGap, ...]


class LiveMarketDataService:
    def __init__(
        self,
        *,
        archive: RawArchive,
        store: MarketDataStore,
        ledger: EventLedger,
        publisher: EventPublisher,
        feed: str,
        gap_detector: MarketGapDetector | None = None,
    ) -> None:
        self.archive = archive
        self.store = store
        self.ledger = ledger
        self.publisher = publisher
        self.feed = feed
        self.gap_detector = gap_detector or MarketGapDetector()

    def ingest_frame(
        self,
        messages: list[dict[str, Any]],
        *,
        received_at: datetime | None = None,
    ) -> LiveFrameSummary:
        observed_at = (received_at or datetime.now(UTC)).astimezone(UTC)
        archived = self.archive.store_json(
            provider="alpaca",
            data_type="stock_stream",
            payload={"messages": messages},
            request_metadata={"feed": self.feed},
            provider_received_at=observed_at,
        )
        raw_object_id = self.store.register_raw_object(archived)
        normalized = [
            value
            for item in messages
            if (
                value := normalize_stream_message(
                    item,
                    feed=self.feed,
                    raw_object_id=raw_object_id,
                    received_at=observed_at,
                )
            )
            is not None
        ]
        bars = tuple(item for item in normalized if isinstance(item, StockBar))
        trades = tuple(item for item in normalized if isinstance(item, StockTrade))
        quotes = tuple(item for item in normalized if isinstance(item, StockQuote))
        gaps: list[MarketDataGap] = []
        for bar in sorted(bars, key=lambda value: value.event_time):
            self.gap_detector.seed(
                bar.symbol,
                self.store.latest_bar_event_time(
                    symbol=bar.symbol,
                    timeframe=bar.timeframe,
                    source=bar.source,
                    feed=bar.feed,
                ),
            )
            gap = self.gap_detector.observe(bar.symbol, bar.event_time)
            if gap is not None:
                gaps.append(gap)
        inserted_bar_ids = self.store.insert_bars(bars, raw_object_id)
        inserted_trade_ids = self.store.insert_trades(trades, raw_object_id)
        inserted_quote_ids = self.store.insert_quotes(quotes, raw_object_id)
        inserted_ids = set(inserted_bar_ids + inserted_trade_ids + inserted_quote_ids)
        events = tuple(
            [
                self._record_event(
                    item,
                    event_type=self._identity_and_event_type(item)[1],
                )
                for item in normalized
            ]
            + [
                self._record_gap_event(gap, raw_object_id=raw_object_id)
                for gap in gaps
            ]
        )
        enqueued = self.ledger.append_batch(events)
        self.ledger.deliver_batch(
            events,
            self.publisher,
            enqueued_event_ids=enqueued,
        )
        return LiveFrameSummary(
            messages_received=len(messages),
            records_inserted=len(inserted_ids),
            ignored_messages=len(messages) - len(normalized),
            gaps=tuple(gaps),
        )

    @staticmethod
    def _identity_and_event_type(item: StockBar | StockTrade | StockQuote) -> tuple[str, str]:
        if isinstance(item, StockBar):
            return item.bar_id, "market.bar.closed.v1"
        if isinstance(item, StockTrade):
            return item.trade_id, "market.trade.received.v1"
        return item.quote_id, "market.quote.received.v1"

    def _record_event(
        self,
        item: StockBar | StockTrade | StockQuote,
        *,
        event_type: str,
    ) -> EventEnvelope:
        if isinstance(item, StockBar):
            business_key = (
                f"{item.symbol}:{item.timeframe}:{item.event_time.isoformat()}:"
                f"{item.source}:{item.feed}"
            )
        elif isinstance(item, StockTrade):
            business_key = (
                f"{item.source}:{item.feed}:{item.symbol}:{item.provider_trade_id}"
            )
        else:
            business_key = f"{item.source}:{item.feed}:{item.quote_fingerprint}"
        return EventEnvelope(
            event_id=self.ledger.stable_event_id(event_type, business_key),
            event_type=event_type,
            event_time=item.event_time,
            emitted_at=datetime.now(UTC),
            producer="market-collector",
            correlation_id=item.raw_object_id,
            payload={**item.model_dump(mode="json"), "dedup_key": business_key},
        )

    def _record_gap_event(
        self,
        gap: MarketDataGap,
        *,
        raw_object_id: str,
    ) -> EventEnvelope:
        business_key = (
            f"{gap.symbol}:{gap.missing_from.isoformat()}:{gap.missing_to.isoformat()}"
        )
        return EventEnvelope(
            event_id=self.ledger.stable_event_id(
                "market.data.gap_detected.v1",
                business_key,
            ),
            event_type="market.data.gap_detected.v1",
            event_time=gap.missing_to,
            emitted_at=datetime.now(UTC),
            producer="market-collector",
            correlation_id=raw_object_id,
            payload={**gap.model_dump(mode="json"), "dedup_key": business_key},
        )
