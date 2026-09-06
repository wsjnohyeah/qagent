from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

import agentic_quant.providers.alpaca_stream as alpaca_stream_module
from agentic_quant.archive import FileRawArchive
from agentic_quant.config import Settings
from agentic_quant.database import event_outbox, market_bars
from agentic_quant.domain import EventEnvelope, StockBar
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.live_ingestion import LiveMarketDataService
from agentic_quant.market_calendar import MarketGapDetector
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.providers.alpaca_stream import (
    AlpacaStockStream,
    AlpacaStreamError,
    normalize_stream_message,
)


class RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, *, event_type: str, event_id: str, envelope_json: str) -> str:
        self.events.append(json.loads(envelope_json))
        return f"1-{len(self.events)}"

    def health(self) -> bool:
        return True


def live_messages() -> list[dict[str, Any]]:
    return [
        {
            "T": "t",
            "S": "AAPL",
            "i": 1234,
            "x": "V",
            "p": 201.25,
            "s": 10,
            "c": ["@"],
            "t": "2026-09-03T14:30:00.123456Z",
            "z": "C",
        },
        {
            "T": "q",
            "S": "AAPL",
            "bx": "V",
            "bp": 201.24,
            "bs": 3,
            "ax": "Q",
            "ap": 201.26,
            "as": 4,
            "c": ["R"],
            "t": "2026-09-03T14:30:00.123457Z",
            "z": "C",
        },
        {
            "T": "b",
            "S": "AAPL",
            "o": 201.0,
            "h": 201.5,
            "l": 200.9,
            "c": 201.25,
            "v": 5000,
            "n": 400,
            "vw": 201.2,
            "t": "2026-09-03T14:30:00Z",
        },
        {"T": "subscription", "trades": ["AAPL"]},
    ]


def test_stream_normalization_uses_receipt_time_for_trade_availability() -> None:
    received_at = datetime(2026, 9, 3, 14, 30, 1, tzinfo=UTC)
    normalized = normalize_stream_message(
        live_messages()[0],
        feed="sip",
        raw_object_id="raw-id",
        received_at=received_at,
    )
    assert normalized is not None
    assert normalized.symbol == "AAPL"
    assert normalized.available_from == received_at


def test_live_frame_archives_persists_publishes_and_deduplicates(
    tmp_path: Path, settings: Settings
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    publisher = RecordingPublisher()
    service = LiveMarketDataService(
        archive=FileRawArchive(tmp_path / "raw"),
        store=MarketDataStore(ledger.engine),
        ledger=ledger,
        publisher=publisher,
        feed="sip",
    )
    received_at = datetime(2026, 9, 3, 14, 30, 1, tzinfo=UTC)
    first = service.ingest_frame(live_messages(), received_at=received_at)
    second = service.ingest_frame(live_messages(), received_at=received_at)
    health = MarketDataStore(ledger.engine).health_summary()

    assert first.messages_received == 4
    assert first.records_inserted == 3
    assert first.ignored_messages == 1
    assert second.records_inserted == 0
    assert len(publisher.events) == 3
    assert health["market_trades"] == 1
    assert health["market_quotes"] == 1
    assert health["market_bars"] == 1
    assert health["raw_objects"] == 1


def test_live_batch_delivery_failure_leaves_every_business_event_recoverable(
    tmp_path: Path, settings: Settings
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)

    class FailFirstPublisher(RecordingPublisher):
        def publish(self, *, event_type: str, event_id: str, envelope_json: str) -> str:
            if not self.events:
                self.events.append(json.loads(envelope_json))
                raise RuntimeError("temporary bus failure")
            return super().publish(
                event_type=event_type,
                event_id=event_id,
                envelope_json=envelope_json,
            )

    flaky = FailFirstPublisher()
    service = LiveMarketDataService(
        archive=FileRawArchive(tmp_path / "raw"),
        store=MarketDataStore(ledger.engine),
        ledger=ledger,
        publisher=flaky,
        feed="sip",
    )
    received_at = datetime(2026, 9, 3, 14, 30, 1, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="temporary bus failure"):
        service.ingest_frame(live_messages(), received_at=received_at)

    assert MarketDataStore(ledger.engine).health_summary()["market_bars"] == 1
    assert len(ledger.recent()) == 3
    assert ledger.outbox_health()["event_outbox_failed"] == 1
    assert ledger.outbox_health()["event_outbox_published"] == 2

    # Replay does not create duplicate business rows or event identities.
    replay = RecordingPublisher()
    replay_service = LiveMarketDataService(
        archive=FileRawArchive(tmp_path / "raw"),
        store=MarketDataStore(ledger.engine),
        ledger=ledger,
        publisher=replay,
        feed="sip",
    )
    assert replay_service.ingest_frame(
        live_messages(), received_at=received_at
    ).records_inserted == 0
    with ledger.engine.begin() as connection:
        connection.execute(
            update(event_outbox)
            .where(event_outbox.c.status == "FAILED")
            .values(next_attempt_at=datetime.now(UTC))
        )
    assert ledger.publish_pending(replay, worker_id="recovery")["published"] == 1
    assert len(ledger.recent()) == 3
    assert ledger.outbox_health()["event_outbox_published"] == 3


def test_live_replay_reuses_legacy_business_id_and_event(
    tmp_path: Path,
    settings: Settings,
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    store = MarketDataStore(ledger.engine)
    received_at = datetime(2026, 9, 3, 14, 31, tzinfo=UTC)
    current = normalize_stream_message(
        live_messages()[2],
        feed="sip",
        raw_object_id="LEGACY_RAW",
        received_at=received_at,
    )
    assert isinstance(current, StockBar)
    legacy = current.model_copy(update={"bar_id": uuid7()})
    store.insert_bars((legacy,), raw_object_id="LEGACY_RAW")
    old_event = EventEnvelope(
        event_id=uuid7(),
        event_type="market.bar.closed.v1",
        event_time=legacy.event_time,
        emitted_at=received_at,
        producer="legacy-collector",
        correlation_id="LEGACY_RAW",
        payload=legacy.model_dump(mode="json"),
    )
    publisher = RecordingPublisher()
    ledger.append(old_event)
    ledger.deliver(old_event, publisher)

    service = LiveMarketDataService(
        archive=FileRawArchive(tmp_path / "raw"),
        store=store,
        ledger=ledger,
        publisher=publisher,
        feed="sip",
    )
    replay = service.ingest_frame([live_messages()[2]], received_at=received_at)

    assert replay.records_inserted == 0
    events = [
        value
        for value in ledger.recent()
        if value["event_type"] == "market.bar.closed.v1"
    ]
    assert len(events) == 1
    with ledger.engine.connect() as connection:
        persisted_ids = set(
            connection.execute(select(market_bars.c.bar_id)).scalars()
        )
    assert events[0]["payload"]["bar_id"] in persisted_ids


def test_gap_detector_uses_exchange_sessions() -> None:
    detector = MarketGapDetector()
    assert detector.observe("AAPL", datetime(2026, 9, 3, 14, 30, tzinfo=UTC)) is None
    gap = detector.observe("AAPL", datetime(2026, 9, 3, 14, 33, tzinfo=UTC))
    assert gap is not None
    assert gap.missing_minutes == 2
    assert gap.missing_from == datetime(2026, 9, 3, 14, 31, tzinfo=UTC)

    # The overnight close-to-open boundary is not an intraday data gap.
    assert detector.observe("AAPL", datetime(2026, 9, 4, 13, 30, tzinfo=UTC)) is None


def test_stream_subscription_channels_are_explicit_and_validated() -> None:
    assert AlpacaStockStream._subscriptions(
        ["bars", "quotes", "bars"], ["SPY"]
    ) == {"bars": ["SPY"], "quotes": ["SPY"]}

    with pytest.raises(ValueError, match="Unsupported stream channels: news"):
        AlpacaStockStream._subscriptions(["bars", "news"], ["SPY"])
    with pytest.raises(ValueError, match="At least one stream channel"):
        AlpacaStockStream._subscriptions([], ["SPY"])


class FakeStreamSocket:
    def __init__(self, market_messages: list[str] | None = None) -> None:
        self.control_messages = [
            json.dumps([{"T": "success", "msg": "connected"}]),
            json.dumps([{"T": "success", "msg": "authenticated"}]),
            json.dumps([{"T": "subscription", "bars": ["SPY"]}]),
        ]
        self.market_messages = market_messages or []

    async def __aenter__(self) -> FakeStreamSocket:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def recv(self) -> str:
        return self.control_messages.pop(0)

    async def send(self, _message: str) -> None:
        return None

    def __aiter__(self) -> FakeStreamSocket:
        return self

    async def __anext__(self) -> str:
        if self.market_messages:
            return self.market_messages.pop(0)
        raise ConnectionError("controlled disconnect")


def test_stream_reconnects_after_a_connection_drops_before_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bar = json.dumps([live_messages()[2]])
    sockets = [FakeStreamSocket(), FakeStreamSocket([bar])]
    calls = 0

    def connect(*_args: object, **_kwargs: object) -> FakeStreamSocket:
        nonlocal calls
        calls += 1
        return sockets.pop(0)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(alpaca_stream_module.websockets, "connect", connect)
    monkeypatch.setattr(alpaca_stream_module.asyncio, "sleep", no_sleep)

    async def scenario() -> list[dict[str, Any]]:
        stream = AlpacaStockStream(api_key="key", api_secret="secret", feed="sip")
        frames = stream.frames(["SPY"], channels=["bars"])
        frame = await anext(frames)
        await frames.aclose()
        return frame

    assert asyncio.run(scenario())[0]["T"] == "b"
    assert calls == 2


def test_stream_exhausts_consecutive_pre_data_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sockets = [FakeStreamSocket() for _ in range(4)]
    calls = 0

    def connect(*_args: object, **_kwargs: object) -> FakeStreamSocket:
        nonlocal calls
        calls += 1
        return sockets.pop(0)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(alpaca_stream_module.websockets, "connect", connect)
    monkeypatch.setattr(alpaca_stream_module.asyncio, "sleep", no_sleep)

    async def scenario() -> None:
        stream = AlpacaStockStream(
            api_key="key",
            api_secret="secret",
            feed="sip",
            max_reconnect_attempts=3,
        )
        await anext(stream.frames(["SPY"], channels=["bars"]))

    with pytest.raises(AlpacaStreamError, match="exhausted reconnect attempts"):
        asyncio.run(scenario())
    assert calls == 4
