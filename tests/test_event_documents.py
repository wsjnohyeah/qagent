from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentic_quant.archive import FileRawArchive
from agentic_quant.document_ingestion import (
    DocumentIngestionService,
    FundamentalsIngestionService,
)
from agentic_quant.document_store import DocumentStore
from agentic_quant.domain import CorporateFact, SourceDocument, SourceTier
from agentic_quant.ledger import EventLedger
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.providers.base import (
    CorporateFactsPage,
    CorporateFactsRequest,
    DocumentFetchRequest,
    DocumentPage,
)
from agentic_quant.providers.documents import (
    AlpacaNewsProvider,
    DocumentProviderConfigurationError,
    FeatureDisabledError,
    InvestorRelationsFeedProvider,
    SecEdgarProvider,
    SocialAggregateProvider,
)


class RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, *, event_type: str, event_id: str, envelope_json: str) -> str:
        self.events.append(json.loads(envelope_json))
        return f"1-{len(self.events)}"

    def health(self) -> bool:
        return True


class DuplicateStoryProvider:
    name = "fixture_documents"

    def __init__(self, observed_at: datetime) -> None:
        self.observed_at = observed_at

    async def fetch_documents_page(
        self,
        request: DocumentFetchRequest,
        *,
        page_token: str | None = None,
    ) -> DocumentPage:
        assert page_token is None
        published = self.observed_at - timedelta(minutes=30)
        documents = (
            SourceDocument(
                document_id="00000000-0000-7000-8000-000000000001",
                provider_document_id="news-1",
                provider="fixture_news",
                canonical_url="https://news.example/apple-quarter",
                source_kind="news",
                source_tier=SourceTier.SECONDARY,
                publisher="Fixture Wire",
                title="Apple reports record quarterly revenue after iPhone demand",
                summary="Revenue reached a new quarterly record.",
                symbols=("AAPL",),
                published_at=published,
                updated_at=published,
                ingested_at=self.observed_at,
                raw_object_id="PENDING_ARCHIVE",
            ),
            SourceDocument(
                document_id="00000000-0000-7000-8000-000000000002",
                provider_document_id="ir-1",
                provider="fixture_ir",
                canonical_url="https://investor.example/apple-quarter",
                source_kind="ir_release",
                source_tier=SourceTier.PRIMARY,
                publisher="Apple Inc.",
                title="Apple reports record quarterly revenue on iPhone demand",
                summary="Official quarterly results.",
                symbols=("AAPL",),
                issuer_name="Apple Inc.",
                cik="0000320193",
                published_at=published + timedelta(minutes=2),
                updated_at=published + timedelta(minutes=3),
                ingested_at=self.observed_at,
                raw_object_id="PENDING_ARCHIVE",
            ),
        )
        return DocumentPage(
            provider=self.name,
            data_type="fixture_documents",
            provider_received_at=self.observed_at,
            request_metadata=request.model_dump(mode="json"),
            raw_payload={"fixture": "duplicate-story"},
            documents=documents,
        )


def _document_service(
    tmp_path: Path,
    settings: Any,
) -> tuple[DocumentIngestionService, DocumentStore, RecordingPublisher]:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    document_store = DocumentStore(ledger.engine)
    publisher = RecordingPublisher()
    service = DocumentIngestionService(
        provider=DuplicateStoryProvider(datetime(2026, 9, 4, 15, tzinfo=UTC)),
        archive=FileRawArchive(tmp_path / "raw"),
        market_store=MarketDataStore(ledger.engine),
        document_store=document_store,
        ledger=ledger,
        publisher=publisher,
    )
    return service, document_store, publisher


def test_pipeline_versions_documents_and_deduplicates_one_catalyst(
    tmp_path: Path,
    settings: Any,
) -> None:
    service, store, publisher = _document_service(tmp_path, settings)
    request = DocumentFetchRequest(symbols=("AAPL",))
    first = asyncio.run(service.ingest_documents(request))
    assert first.documents_inserted == 2
    assert first.versions_inserted == 2
    assert first.catalysts_inserted == 1
    assert first.catalyst_links_inserted == 2
    assert [event["event_type"] for event in publisher.events].count(
        "catalyst.normalized.v1"
    ) == 1
    assert store.health_summary() == {
        "source_documents": 2,
        "document_versions": 2,
        "entities": 1,
        "catalysts": 1,
        "corporate_facts": 0,
    }
    catalyst = store.recent_catalysts()[0]
    assert catalyst["source_count"] == 2
    assert catalyst["primary_source_document_id"] is not None
    matches = store.search_documents(query="quarterly", symbol="AAPL")
    assert len(matches) == 2
    assert {match["source_tier"] for match in matches} == {"primary", "secondary"}
    assert any(match["corrected_at"] is not None for match in matches)

    replay_service, replay_store, replay_publisher = _document_service(tmp_path, settings)
    replay = asyncio.run(replay_service.ingest_documents(request))
    assert replay.documents_inserted == 0
    assert replay.versions_inserted == 0
    assert replay.catalysts_inserted == 0
    assert replay.catalyst_links_inserted == 0
    assert replay_publisher.events == []
    assert replay_store.health_summary()["catalysts"] == 1


def test_alpaca_news_adapter_retains_publication_and_correction_times() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1beta1/news"
        assert request.url.params["symbols"] == "AAPL"
        return httpx.Response(
            200,
            json={
                "news": [
                    {
                        "id": 42,
                        "headline": "Apple reports quarterly revenue",
                        "summary": "Results released.",
                        "content": "<p>Primary details summarized by the wire.</p>",
                        "created_at": "2026-09-03T20:00:00Z",
                        "updated_at": "2026-09-03T20:05:00Z",
                        "url": "https://news.example/42",
                        "symbols": ["AAPL"],
                        "source": "Fixture Wire",
                    }
                ],
                "next_page_token": None,
            },
        )

    async def scenario() -> DocumentPage:
        client = httpx.AsyncClient(
            base_url="https://data.alpaca.markets",
            transport=httpx.MockTransport(handler),
        )
        provider = AlpacaNewsProvider(
            api_key="test-key",
            api_secret="test-secret",
            client=client,
        )
        page = await provider.fetch_documents_page(
            DocumentFetchRequest(symbols=("AAPL",))
        )
        await client.aclose()
        return page

    document = asyncio.run(scenario()).documents[0]
    assert document.source_tier == SourceTier.SECONDARY
    assert document.published_at == datetime(2026, 9, 3, 20, tzinfo=UTC)
    assert document.updated_at == datetime(2026, 9, 3, 20, 5, tzinfo=UTC)
    assert document.body_text == "Primary details summarized by the wire."


def test_entity_resolution_supports_multiple_symbols_for_one_cik(settings: Any) -> None:
    upgrade_database(settings.database_url)
    store = DocumentStore(EventLedger(settings.database_url).engine)
    now = datetime(2026, 9, 3, 20, tzinfo=UTC)
    for index, symbol in enumerate(("BRK.A", "BRK.B"), start=1):
        store.upsert_document(
            SourceDocument(
                document_id=f"00000000-0000-7000-8000-{index:012d}",
                provider_document_id=f"filing-{index}",
                provider="sec_edgar",
                canonical_url=f"https://www.sec.gov/Archives/fixture-{index}",
                source_kind="sec_filing",
                source_tier=SourceTier.PRIMARY,
                publisher="U.S. Securities and Exchange Commission",
                title=f"Berkshire Hathaway filing for {symbol}",
                symbols=(symbol,),
                issuer_name="Berkshire Hathaway Inc.",
                cik="0001067983",
                published_at=now,
                ingested_at=now,
                raw_object_id="raw-fixture",
            ),
            "raw-fixture",
        )
    assert store.health_summary()["entities"] == 1
    assert len(store.search_documents(query="Berkshire", symbol="BRK.A")) == 1
    assert len(store.search_documents(query="Berkshire", symbol="BRK.B")) == 1


def test_provider_correction_time_creates_an_immutable_version(settings: Any) -> None:
    upgrade_database(settings.database_url)
    store = DocumentStore(EventLedger(settings.database_url).engine)
    published = datetime(2026, 9, 3, 20, tzinfo=UTC)
    original = SourceDocument(
        document_id="00000000-0000-7000-8000-000000000020",
        provider_document_id="news-correction",
        provider="fixture_news",
        canonical_url="https://news.example/correction",
        source_kind="news",
        source_tier=SourceTier.SECONDARY,
        publisher="Fixture Wire",
        title="Example reports quarterly revenue",
        symbols=("EXM",),
        published_at=published,
        updated_at=published,
        ingested_at=published + timedelta(minutes=1),
        raw_object_id="raw-fixture",
    )
    first = store.upsert_document(original, "raw-fixture")
    corrected = original.model_copy(
        update={
            "document_id": "00000000-0000-7000-8000-000000000021",
            "updated_at": published + timedelta(minutes=2),
            "ingested_at": published + timedelta(minutes=3),
        }
    )
    second = store.upsert_document(corrected, "raw-fixture")
    assert first.version_inserted is True
    assert second.document_inserted is False
    assert second.version_inserted is True
    assert store.health_summary()["source_documents"] == 1
    assert store.health_summary()["document_versions"] == 2
    latest = store.search_documents(query="quarterly", symbol="EXM")[0]
    assert latest["corrected_at"] == published + timedelta(minutes=2)


def test_sec_adapters_normalize_primary_filings_and_company_facts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "research@example.com" in request.headers["User-Agent"]
        if request.url.path.startswith("/submissions/"):
            return httpx.Response(
                200,
                json={
                    "cik": "320193",
                    "name": "Apple Inc.",
                    "filings": {
                        "recent": {
                            "accessionNumber": ["0000320193-26-000001"],
                            "form": ["8-K"],
                            "primaryDocument": ["aapl-20260903.htm"],
                            "acceptanceDateTime": ["20260903160500"],
                            "filingDate": ["2026-09-03"],
                            "reportDate": ["2026-09-03"],
                            "primaryDocDescription": ["Current report"],
                        }
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2026-06-29",
                                        "end": "2026-09-27",
                                        "val": 100000000000,
                                        "accn": "0000320193-26-000001",
                                        "fy": 2026,
                                        "fp": "Q4",
                                        "form": "10-K",
                                        "filed": "2026-10-30",
                                    }
                                ]
                            }
                        }
                    }
                },
            },
        )

    async def scenario() -> tuple[DocumentPage, CorporateFactsPage]:
        client = httpx.AsyncClient(
            base_url="https://data.sec.gov",
            transport=httpx.MockTransport(handler),
        )
        provider = SecEdgarProvider(
            user_agent="Agentic Quant research@example.com",
            client=client,
        )
        filings = await provider.fetch_documents_page(
            DocumentFetchRequest(
                symbols=("AAPL",),
                cik="320193",
                forms=("8-K",),
            )
        )
        facts = await provider.fetch_company_facts(
            CorporateFactsRequest(symbol="AAPL", cik="320193")
        )
        await client.aclose()
        return filings, facts

    filings, facts = asyncio.run(scenario())
    assert filings.documents[0].source_tier == SourceTier.PRIMARY
    assert filings.documents[0].cik == "0000320193"
    assert filings.documents[0].published_at == datetime(2026, 9, 3, 16, 5, tzinfo=UTC)
    assert facts.facts[0].tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert facts.facts[0].numeric_value == Decimal("100000000000")
    assert facts.facts[0].available_from == facts.provider_received_at


def test_fundamentals_pipeline_is_idempotent(tmp_path: Path, settings: Any) -> None:
    upgrade_database(settings.database_url)
    now = datetime(2026, 10, 30, tzinfo=UTC)
    fact = CorporateFact(
        fact_id="00000000-0000-7000-8000-000000000010",
        fact_fingerprint="a" * 64,
        symbol="AAPL",
        cik="0000320193",
        issuer_name="Apple Inc.",
        taxonomy="us-gaap",
        tag="Revenue",
        unit="USD",
        period_end=datetime(2026, 9, 27, tzinfo=UTC),
        filed_at=now,
        form="10-K",
        numeric_value=Decimal("100"),
        value_text="100",
        available_from=now,
        raw_object_id="PENDING_ARCHIVE",
        ingested_at=now,
    )
    page = CorporateFactsPage(
        provider="sec_edgar",
        data_type="company_facts",
        provider_received_at=now,
        request_metadata={"symbol": "AAPL"},
        raw_payload={"fixture": "facts"},
        facts=(fact,),
    )
    ledger = EventLedger(settings.database_url)
    publisher = RecordingPublisher()
    service = FundamentalsIngestionService(
        archive=FileRawArchive(tmp_path / "raw"),
        market_store=MarketDataStore(ledger.engine),
        document_store=DocumentStore(ledger.engine),
        ledger=ledger,
        publisher=publisher,
    )
    assert service.ingest_page(page).facts_inserted == 1
    assert service.ingest_page(page).facts_inserted == 0
    assert len(publisher.events) == 1


def test_ir_adapter_requires_approved_host_and_social_defaults_off() -> None:
    with pytest.raises(DocumentProviderConfigurationError):
        InvestorRelationsFeedProvider(
            feed_url="https://investor.example/feed.xml",
            expected_hostname="different.example",
            issuer_name="Example Inc.",
        )
    with pytest.raises(FeatureDisabledError):
        SocialAggregateProvider(
            enabled=False,
            endpoint="https://social.example/v1/aggregates",
            token="unused",
        )


def test_ir_adapter_parses_an_approved_primary_source() -> None:
    feed = """<?xml version="1.0"?>
    <rss><channel><item>
      <guid>release-1</guid>
      <title>Example announces quarterly results</title>
      <link>https://investor.example/releases/1</link>
      <description><![CDATA[<p>Revenue increased.</p>]]></description>
      <pubDate>Thu, 03 Sep 2026 20:00:00 GMT</pubDate>
    </item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://investor.example/feed.xml"
        return httpx.Response(200, text=feed)

    async def scenario() -> DocumentPage:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = InvestorRelationsFeedProvider(
            feed_url="https://investor.example/feed.xml",
            expected_hostname="investor.example",
            issuer_name="Example Inc.",
            client=client,
        )
        page = await provider.fetch_documents_page(
            DocumentFetchRequest(symbols=("EXM",))
        )
        await client.aclose()
        return page

    document = asyncio.run(scenario()).documents[0]
    assert document.source_tier == SourceTier.PRIMARY
    assert document.provider_document_id == "release-1"
    assert document.summary == "Revenue increased."
    assert document.published_at == datetime(2026, 9, 3, 20, tzinfo=UTC)
