from __future__ import annotations

from datetime import UTC, datetime
from pydantic import BaseModel, ConfigDict

from agentic_quant.archive import RawArchive
from agentic_quant.data_quality import MarketDataQualityService
from agentic_quant.domain import EventEnvelope, StockBar
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.market_store import MarketDataStore
from agentic_quant.providers.base import EquityMarketDataProvider, EventPublisher, StockBarsRequest


class IngestionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    ingestion_run_id: str
    provider: str
    symbol: str
    pages_received: int
    records_received: int
    records_inserted: int
    status: str


class MarketDataIngestionService:
    def __init__(
        self,
        *,
        provider: EquityMarketDataProvider,
        archive: RawArchive,
        store: MarketDataStore,
        ledger: EventLedger,
        publisher: EventPublisher,
        calendar_name: str = "XNYS",
        code_git_sha: str = "UNAVAILABLE",
    ) -> None:
        self.provider = provider
        self.archive = archive
        self.store = store
        self.ledger = ledger
        self.publisher = publisher
        self.code_git_sha = code_git_sha
        self.data_quality = MarketDataQualityService(
            store.engine,
            ledger,
            calendar_name=calendar_name,
        )

    async def ingest_stock_bars(self, request: StockBarsRequest) -> IngestionSummary:
        requested_at = datetime.now(UTC)
        run_id = uuid7()
        request_metadata = request.model_dump(mode="json")
        data_type = f"stock_bars_{request.timeframe.casefold()}"
        self.store.start_run(
            ingestion_run_id=run_id,
            provider=self.provider.name,
            data_type=data_type,
            requested_at=requested_at,
            request_metadata=request_metadata,
        )
        pages_received = 0
        records_received = 0
        records_inserted = 0
        page_token: str | None = None
        seen_tokens: set[str] = set()
        try:
            while True:
                page = await self.provider.fetch_stock_bars_page(request, page_token=page_token)
                pages_received += 1
                records_received += len(page.bars)
                archived = self.archive.store_json(
                    provider=page.provider,
                    data_type=data_type,
                    payload=page.raw_payload,
                    request_metadata=page.request_metadata,
                    provider_received_at=page.provider_received_at,
                )
                raw_object_id = self.store.register_raw_object(archived)
                inserted_ids = self.store.insert_bars(page.bars, raw_object_id)
                records_inserted += len(inserted_ids)
                canonical_bars = self.store.canonical_bars(page.bars)
                events = tuple(
                    self._bar_event(
                        bar,
                        run_id=run_id,
                    )
                    for bar in canonical_bars
                )
                enqueued = self.ledger.append_batch(
                    events,
                    reconcile_business_ids=any(
                        incoming.bar_id != stored.bar_id
                        for incoming, stored in zip(
                            page.bars,
                            canonical_bars,
                            strict=True,
                        )
                    ),
                )
                self.ledger.deliver_batch(
                    events,
                    self.publisher,
                    enqueued_event_ids=enqueued,
                )
                page_token = page.next_page_token
                if page_token is None:
                    break
                if page_token in seen_tokens:
                    raise RuntimeError("Provider returned a repeated pagination token")
                seen_tokens.add(page_token)
            stored_bars = self.store.bars_between(
                symbol=request.symbol,
                timeframe=request.timeframe,
                start=request.start,
                end=request.end,
                source=self.provider.name,
                feed=request.feed,
            )
            self.data_quality.require_bars(
                stored_bars,
                symbol=request.symbol,
                timeframe=request.timeframe,
                code_git_sha=self.code_git_sha,
                expected_start=request.start,
                expected_end=request.end,
            )
        except Exception as exc:
            self.store.finish_run(
                ingestion_run_id=run_id,
                status="FAILED",
                completed_at=datetime.now(UTC),
                pages_received=pages_received,
                records_received=records_received,
                records_inserted=records_inserted,
                error_code=type(exc).__name__,
            )
            raise
        self.store.finish_run(
            ingestion_run_id=run_id,
            status="COMPLETED",
            completed_at=datetime.now(UTC),
            pages_received=pages_received,
            records_received=records_received,
            records_inserted=records_inserted,
        )
        return IngestionSummary(
            ingestion_run_id=run_id,
            provider=self.provider.name,
            symbol=request.symbol.upper(),
            pages_received=pages_received,
            records_received=records_received,
            records_inserted=records_inserted,
            status="COMPLETED",
        )

    def _bar_event(self, bar: StockBar, *, run_id: str) -> EventEnvelope:
        business_key = (
            f"{bar.symbol}:{bar.timeframe}:{bar.event_time.isoformat()}:"
            f"{bar.source}:{bar.feed}"
        )
        return EventEnvelope(
            event_id=self.ledger.stable_event_id("market.bar.closed.v1", business_key),
            event_type="market.bar.closed.v1",
            event_time=bar.event_time,
            emitted_at=datetime.now(UTC),
            producer="market-collector",
            correlation_id=run_id,
            payload={
                **bar.model_dump(mode="json"),
                "dedup_key": business_key,
            },
        )
