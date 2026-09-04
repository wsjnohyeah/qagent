from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from agentic_quant.archive import FileRawArchive
from agentic_quant.config import Settings
from agentic_quant.domain import StockBar
from agentic_quant.ledger import EventLedger
from agentic_quant.market_ingestion import MarketDataIngestionService
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.providers.alpaca import AlpacaMarketDataProvider
from agentic_quant.providers.base import OptionChainRequest, StockBarsPage, StockBarsRequest


def test_alpaca_adapter_normalizes_raw_one_minute_bars() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["feed"] = request.url.params["feed"]
        captured["adjustment"] = request.url.params["adjustment"]
        captured["key"] = request.headers["APCA-API-KEY-ID"]
        return httpx.Response(
            200,
            json={
                "bars": [
                    {
                        "t": "2026-09-03T14:30:00Z",
                        "o": 100.1,
                        "h": 101.2,
                        "l": 99.9,
                        "c": 101.0,
                        "v": 12345,
                        "n": 234,
                        "vw": 100.75,
                    }
                ],
                "next_page_token": None,
                "symbol": "AAPL",
            },
        )

    async def scenario() -> StockBarsPage:
        client = httpx.AsyncClient(
            base_url="https://data.alpaca.markets",
            transport=httpx.MockTransport(handler),
        )
        provider = AlpacaMarketDataProvider(
            api_key="test-key",
            api_secret="test-secret",
            client=client,
        )
        result = await provider.fetch_stock_bars_page(
            StockBarsRequest(
                symbol="aapl",
                start=datetime(2026, 9, 3, 14, 30, tzinfo=UTC),
                end=datetime(2026, 9, 3, 14, 31, tzinfo=UTC),
            )
        )
        await client.aclose()
        return result

    page = asyncio.run(scenario())
    assert captured == {
        "path": "/v2/stocks/AAPL/bars",
        "feed": "sip",
        "adjustment": "raw",
        "key": "test-key",
    }
    assert page.raw_payload["symbol"] == "AAPL"
    assert len(page.bars) == 1
    assert page.bars[0].close == Decimal("101.0")
    assert page.bars[0].available_from == datetime(2026, 9, 3, 14, 31, tzinfo=UTC)


def test_alpaca_adapter_supports_point_in_time_safe_daily_bars() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["timeframe"] == "1Day"
        return httpx.Response(
            200,
            json={
                "bars": [
                    {
                        "t": "2026-09-02T04:00:00Z",
                        "o": 100,
                        "h": 102,
                        "l": 99,
                        "c": 101,
                        "v": 1000,
                        "n": 100,
                        "vw": 100.5,
                    }
                ],
                "next_page_token": None,
            },
        )

    async def scenario() -> StockBarsPage:
        client = httpx.AsyncClient(
            base_url="https://data.alpaca.markets",
            transport=httpx.MockTransport(handler),
        )
        provider = AlpacaMarketDataProvider(
            api_key="test-key",
            api_secret="test-secret",
            client=client,
        )
        result = await provider.fetch_stock_bars_page(
            StockBarsRequest(
                symbol="AAPL",
                start=datetime(2026, 9, 2, tzinfo=UTC),
                end=datetime(2026, 9, 4, tzinfo=UTC),
                timeframe="1Day",
            )
        )
        await client.aclose()
        return result

    page = asyncio.run(scenario())
    assert page.bars[0].timeframe == "1Day"
    assert page.bars[0].available_from == datetime(2026, 9, 3, 4, tzinfo=UTC)


def test_alpaca_adapter_normalizes_option_snapshot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1beta1/options/snapshots/AAPL"
        assert request.url.params["feed"] == "opra"
        return httpx.Response(
            200,
            json={
                "snapshots": {
                    "AAPL260918C00200000": {
                        "latestQuote": {
                            "t": "2026-09-03T19:59:59Z",
                            "bp": 12.3,
                            "bs": 2,
                            "ap": 12.5,
                            "as": 3,
                            "bx": "C",
                            "ax": "W",
                        },
                        "latestTrade": {
                            "t": "2026-09-03T19:59:58Z",
                            "p": 12.4,
                            "s": 1,
                            "x": "C",
                        },
                        "impliedVolatility": 0.31,
                        "greeks": {"delta": 0.55, "gamma": 0.02, "theta": -0.04},
                    }
                },
                "next_page_token": None,
            },
        )

    async def scenario():  # type: ignore[no-untyped-def]
        client = httpx.AsyncClient(
            base_url="https://data.alpaca.markets",
            transport=httpx.MockTransport(handler),
        )
        provider = AlpacaMarketDataProvider(
            api_key="test-key",
            api_secret="test-secret",
            client=client,
        )
        page = await provider.fetch_option_snapshots_page(
            OptionChainRequest(underlying_symbol="aapl")
        )
        await client.aclose()
        return page

    page = asyncio.run(scenario())
    assert len(page.snapshots) == 1
    snapshot = page.snapshots[0]
    assert snapshot.contract_symbol == "AAPL260918C00200000"
    assert snapshot.bid_price == Decimal("12.3")
    assert snapshot.delta == Decimal("0.55")


class FakeProvider:
    name = "alpaca"

    def __init__(self) -> None:
        self.calls = 0

    async def fetch_stock_bars_page(
        self,
        request: StockBarsRequest,
        *,
        page_token: str | None = None,
    ) -> StockBarsPage:
        self.calls += 1
        event_time = request.start + timedelta(minutes=self.calls - 1)
        raw = {
            "bars": [{"t": event_time.isoformat(), "c": 100 + self.calls}],
            "page": self.calls,
        }
        return StockBarsPage(
            provider="alpaca",
            provider_received_at=datetime(2026, 9, 4, 1, self.calls, tzinfo=UTC),
            request_metadata={"page_token": page_token},
            raw_payload=raw,
            bars=(
                StockBar(
                    bar_id=f"00000000-0000-7000-8000-{self.calls:012d}",
                    symbol=request.symbol.upper(),
                    timeframe="1Min",
                    event_time=event_time,
                    available_from=event_time + timedelta(minutes=1),
                    open=Decimal("100"),
                    high=Decimal("102"),
                    low=Decimal("99"),
                    close=Decimal(str(100 + self.calls)),
                    volume=1000,
                    trade_count=10,
                    vwap=Decimal("100.5"),
                    source="alpaca",
                    feed=request.feed,
                    raw_object_id="PENDING_ARCHIVE",
                    ingested_at=datetime(2026, 9, 4, 1, self.calls, tzinfo=UTC),
                ),
            ),
            next_page_token="page-2" if self.calls == 1 else None,
        )


class RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, *, event_type: str, event_id: str, envelope_json: str) -> str:
        self.events.append(json.loads(envelope_json))
        return f"1-{len(self.events)}"

    def health(self) -> bool:
        return True


def _ingestion_service(
    tmp_path: Path, settings: Settings
) -> tuple[MarketDataIngestionService, FakeProvider, RecordingPublisher, MarketDataStore]:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    provider = FakeProvider()
    publisher = RecordingPublisher()
    service = MarketDataIngestionService(
        provider=provider,
        archive=FileRawArchive(tmp_path / "raw"),
        store=MarketDataStore(ledger.engine),
        ledger=ledger,
        publisher=publisher,
    )
    return service, provider, publisher, MarketDataStore(ledger.engine)


def test_historical_pipeline_archives_normalizes_publishes_and_deduplicates(
    tmp_path: Path, settings: Settings
) -> None:
    service, _, publisher, store = _ingestion_service(tmp_path, settings)
    request = StockBarsRequest(
        symbol="AAPL",
        start=datetime(2026, 9, 3, 14, 30, tzinfo=UTC),
        end=datetime(2026, 9, 3, 14, 32, tzinfo=UTC),
    )
    first = asyncio.run(service.ingest_stock_bars(request))
    assert first.pages_received == 2
    assert first.records_inserted == 2
    assert len(publisher.events) == 2
    assert store.health_summary()["market_bars"] == 2
    assert store.health_summary()["raw_objects"] == 2

    service2, _, publisher2, store2 = _ingestion_service(tmp_path, settings)
    second = asyncio.run(service2.ingest_stock_bars(request))
    assert second.records_received == 2
    assert second.records_inserted == 0
    assert publisher2.events == []
    assert store2.health_summary()["market_bars"] == 2
    assert store2.health_summary()["raw_objects"] == 2


def test_file_archive_is_content_addressed(tmp_path: Path) -> None:
    archive = FileRawArchive(tmp_path)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    first = archive.store_json(
        provider="alpaca",
        data_type="stock_bars_1m",
        payload={"b": 2, "a": 1},
        request_metadata={},
        provider_received_at=now,
    )
    second = archive.store_json(
        provider="alpaca",
        data_type="stock_bars_1m",
        payload={"a": 1, "b": 2},
        request_metadata={},
        provider_received_at=now,
    )
    assert first.content_sha256 == second.content_sha256
    assert first.uri == second.uri
    assert Path(unquote(urlparse(first.uri).path)).exists()
