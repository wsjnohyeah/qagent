from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from agentic_quant.archive import RawArchive
from agentic_quant.domain import EventEnvelope
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.market_store import MarketDataStore
from agentic_quant.providers.base import (
    EventPublisher,
    OptionChainRequest,
    OptionMarketDataProvider,
)


class OptionIngestionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    ingestion_run_id: str
    provider: str
    underlying_symbol: str
    pages_received: int
    records_received: int
    records_inserted: int
    truncated: bool
    status: str


class OptionDataIngestionService:
    def __init__(
        self,
        *,
        provider: OptionMarketDataProvider,
        archive: RawArchive,
        store: MarketDataStore,
        ledger: EventLedger,
        publisher: EventPublisher,
    ) -> None:
        self.provider = provider
        self.archive = archive
        self.store = store
        self.ledger = ledger
        self.publisher = publisher

    async def ingest_option_chain(self, request: OptionChainRequest) -> OptionIngestionSummary:
        requested_at = datetime.now(UTC)
        run_id = uuid7()
        self.store.start_run(
            ingestion_run_id=run_id,
            provider=self.provider.name,
            data_type="option_chain_snapshot",
            requested_at=requested_at,
            request_metadata=request.model_dump(mode="json"),
        )
        pages_received = records_received = records_inserted = 0
        truncated = False
        page_token: str | None = None
        seen_tokens: set[str] = set()
        try:
            while True:
                page = await self.provider.fetch_option_snapshots_page(
                    request,
                    page_token=page_token,
                )
                pages_received += 1
                records_received += len(page.snapshots)
                archived = self.archive.store_json(
                    provider=page.provider,
                    data_type="option_chain_snapshot",
                    payload=page.raw_payload,
                    request_metadata=page.request_metadata,
                    provider_received_at=page.provider_received_at,
                )
                raw_object_id = self.store.register_raw_object(archived)
                inserted_ids = self.store.insert_option_snapshots(page.snapshots, raw_object_id)
                records_inserted += len(inserted_ids)
                for snapshot in page.snapshots:
                    if snapshot.option_snapshot_id not in inserted_ids:
                        continue
                    event = EventEnvelope(
                        event_id=uuid7(),
                        event_type="option.snapshot.received.v1",
                        event_time=snapshot.as_of,
                        emitted_at=datetime.now(UTC),
                        producer="market-collector",
                        correlation_id=run_id,
                        payload=snapshot.model_copy(
                            update={"raw_object_id": raw_object_id}
                        ).model_dump(mode="json"),
                    )
                    self.ledger.append(event)
                    self.publisher.publish(
                        event_type=event.event_type,
                        event_id=event.event_id,
                        envelope_json=event.model_dump_json(),
                    )
                page_token = page.next_page_token
                if page_token is None:
                    break
                if pages_received >= request.max_pages:
                    truncated = True
                    break
                if page_token in seen_tokens:
                    raise RuntimeError("Provider returned a repeated pagination token")
                seen_tokens.add(page_token)
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
        return OptionIngestionSummary(
            ingestion_run_id=run_id,
            provider=self.provider.name,
            underlying_symbol=request.underlying_symbol.upper(),
            pages_received=pages_received,
            records_received=records_received,
            records_inserted=records_inserted,
            truncated=truncated,
            status="COMPLETED",
        )
