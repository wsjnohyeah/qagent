from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from agentic_quant.archive import RawArchive
from agentic_quant.document_store import DocumentStore, classify_catalyst
from agentic_quant.domain import EventEnvelope, SourceDocument
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.market_store import MarketDataStore
from agentic_quant.providers.base import (
    CorporateFactsPage,
    DocumentFetchRequest,
    DocumentProvider,
    EventPublisher,
)


class DocumentIngestionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    ingestion_run_id: str
    provider: str
    pages_received: int
    documents_received: int
    documents_inserted: int
    versions_inserted: int
    catalysts_inserted: int
    catalyst_links_inserted: int
    truncated: bool
    status: str


class FundamentalsIngestionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    ingestion_run_id: str
    provider: str
    symbol: str
    facts_received: int
    facts_inserted: int
    status: str


class DocumentIngestionService:
    def __init__(
        self,
        *,
        provider: DocumentProvider,
        archive: RawArchive,
        market_store: MarketDataStore,
        document_store: DocumentStore,
        ledger: EventLedger,
        publisher: EventPublisher,
    ) -> None:
        self.provider = provider
        self.archive = archive
        self.market_store = market_store
        self.document_store = document_store
        self.ledger = ledger
        self.publisher = publisher

    async def ingest_documents(
        self,
        request: DocumentFetchRequest,
    ) -> DocumentIngestionSummary:
        requested_at = datetime.now(UTC)
        run_id = uuid7()
        self.market_store.start_run(
            ingestion_run_id=run_id,
            provider=self.provider.name,
            data_type="source_documents",
            requested_at=requested_at,
            request_metadata=request.model_dump(mode="json"),
        )
        pages_received = documents_received = documents_inserted = 0
        versions_inserted = catalysts_inserted = catalyst_links_inserted = 0
        page_token: str | None = None
        seen_tokens: set[str] = set()
        truncated = False
        try:
            while True:
                page = await self.provider.fetch_documents_page(
                    request,
                    page_token=page_token,
                )
                pages_received += 1
                documents_received += len(page.documents)
                archived = self.archive.store_json(
                    provider=page.provider,
                    data_type=page.data_type,
                    payload=page.raw_payload,
                    request_metadata=page.request_metadata,
                    provider_received_at=page.provider_received_at,
                )
                raw_object_id = self.market_store.register_raw_object(archived)
                page_events: list[EventEnvelope] = []
                for document in page.documents:
                    normalized = document.model_copy(
                        update={"raw_object_id": raw_object_id}
                    )
                    write = self.document_store.upsert_document(normalized, raw_object_id)
                    documents_inserted += int(write.document_inserted)
                    versions_inserted += int(write.version_inserted)
                    stored_document = normalized.model_copy(
                        update={"document_id": write.document_id}
                    )
                    page_events.append(
                        self._record_document_event(stored_document, run_id=run_id)
                    )
                    if not stored_document.symbols:
                        continue
                    catalyst = self.document_store.resolve_catalyst(
                        stored_document,
                        document_id=write.document_id,
                    )
                    catalysts_inserted += int(catalyst.inserted)
                    catalyst_links_inserted += int(catalyst.document_linked)
                    page_events.append(
                        self._record_catalyst_event(
                            stored_document,
                            catalyst_id=catalyst.catalyst_id,
                            run_id=run_id,
                        )
                    )
                events = tuple(page_events)
                enqueued = self.ledger.append_batch(events)
                self.ledger.deliver_batch(
                    events,
                    self.publisher,
                    enqueued_event_ids=enqueued,
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
            self.market_store.finish_run(
                ingestion_run_id=run_id,
                status="FAILED",
                completed_at=datetime.now(UTC),
                pages_received=pages_received,
                records_received=documents_received,
                records_inserted=versions_inserted,
                error_code=type(exc).__name__,
            )
            raise
        self.market_store.finish_run(
            ingestion_run_id=run_id,
            status="COMPLETED",
            completed_at=datetime.now(UTC),
            pages_received=pages_received,
            records_received=documents_received,
            records_inserted=versions_inserted,
        )
        return DocumentIngestionSummary(
            ingestion_run_id=run_id,
            provider=self.provider.name,
            pages_received=pages_received,
            documents_received=documents_received,
            documents_inserted=documents_inserted,
            versions_inserted=versions_inserted,
            catalysts_inserted=catalysts_inserted,
            catalyst_links_inserted=catalyst_links_inserted,
            truncated=truncated,
            status="COMPLETED",
        )

    def _record_document_event(
        self,
        document: SourceDocument,
        *,
        run_id: str,
    ) -> EventEnvelope:
        business_key = (
            f"{document.provider}:{document.provider_document_id}:"
            f"{document.updated_at or document.published_at}:"
            f"{document.title}"
        )
        return EventEnvelope(
            event_id=self.ledger.stable_event_id("document.ingested.v1", business_key),
            event_type="document.ingested.v1",
            event_time=document.published_at,
            emitted_at=datetime.now(UTC),
            producer="event-collector",
            correlation_id=run_id,
            payload={
                "document_id": document.document_id,
                "provider_document_id": document.provider_document_id,
                "provider": document.provider,
                "source_kind": document.source_kind,
                "source_tier": document.source_tier,
                "symbols": document.symbols,
                "published_at": document.published_at.isoformat(),
                "corrected_at": (
                    document.updated_at.isoformat()
                    if document.updated_at
                    and document.updated_at > document.published_at
                    else None
                ),
                "ingested_at": document.ingested_at.isoformat(),
                "raw_object_id": document.raw_object_id,
            },
        )

    def _record_catalyst_event(
        self,
        document: SourceDocument,
        *,
        catalyst_id: str,
        run_id: str,
    ) -> EventEnvelope:
        return EventEnvelope(
            event_id=self.ledger.stable_event_id(
                "catalyst.normalized.v1",
                catalyst_id,
            ),
            event_type="catalyst.normalized.v1",
            event_time=document.published_at,
            emitted_at=datetime.now(UTC),
            producer="event-collector",
            correlation_id=run_id,
            payload={
                "catalyst_id": catalyst_id,
                "document_id": document.document_id,
                "primary_symbol": document.symbols[0],
                "headline": document.title,
                "catalyst_type": classify_catalyst(document),
                "source_tier": document.source_tier,
                "available_from": document.ingested_at.isoformat(),
            },
        )


class FundamentalsIngestionService:
    def __init__(
        self,
        *,
        archive: RawArchive,
        market_store: MarketDataStore,
        document_store: DocumentStore,
        ledger: EventLedger,
        publisher: EventPublisher,
    ) -> None:
        self.archive = archive
        self.market_store = market_store
        self.document_store = document_store
        self.ledger = ledger
        self.publisher = publisher

    def ingest_page(self, page: CorporateFactsPage) -> FundamentalsIngestionSummary:
        run_id = uuid7()
        self.market_store.start_run(
            ingestion_run_id=run_id,
            provider=page.provider,
            data_type=page.data_type,
            requested_at=page.provider_received_at,
            request_metadata=page.request_metadata,
        )
        try:
            archived = self.archive.store_json(
                provider=page.provider,
                data_type=page.data_type,
                payload=page.raw_payload,
                request_metadata=page.request_metadata,
                provider_received_at=page.provider_received_at,
            )
            raw_object_id = self.market_store.register_raw_object(archived)
            inserted_ids = self.document_store.insert_facts(page.facts, raw_object_id)
            fact_ids = self.document_store.fact_ids_for_fingerprints(
                tuple(fact.fact_fingerprint for fact in page.facts)
            )
            events: list[EventEnvelope] = []
            for fact in page.facts:
                normalized = fact.model_copy(
                    update={
                        "fact_id": fact_ids[fact.fact_fingerprint],
                        "raw_object_id": raw_object_id,
                    }
                )
                events.append(EventEnvelope(
                    event_id=self.ledger.stable_event_id(
                        "fundamental.fact.received.v1",
                        normalized.fact_fingerprint,
                    ),
                    event_type="fundamental.fact.received.v1",
                    event_time=normalized.available_from,
                    emitted_at=datetime.now(UTC),
                    producer="event-collector",
                    correlation_id=run_id,
                    payload=normalized.model_dump(mode="json"),
                ))
            batch = tuple(events)
            enqueued = self.ledger.append_batch(batch)
            self.ledger.deliver_batch(
                batch,
                self.publisher,
                enqueued_event_ids=enqueued,
            )
        except Exception as exc:
            self.market_store.finish_run(
                ingestion_run_id=run_id,
                status="FAILED",
                completed_at=datetime.now(UTC),
                pages_received=1,
                records_received=len(page.facts),
                records_inserted=0,
                error_code=type(exc).__name__,
            )
            raise
        self.market_store.finish_run(
            ingestion_run_id=run_id,
            status="COMPLETED",
            completed_at=datetime.now(UTC),
            pages_received=1,
            records_received=len(page.facts),
            records_inserted=len(inserted_ids),
        )
        return FundamentalsIngestionSummary(
            ingestion_run_id=run_id,
            provider=page.provider,
            symbol=page.facts[0].symbol if page.facts else "UNKNOWN",
            facts_received=len(page.facts),
            facts_inserted=len(inserted_ids),
            status="COMPLETED",
        )
