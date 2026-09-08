from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import update

from agentic_quant.archive import FileRawArchive
from agentic_quant.api import create_app
from agentic_quant.backtest_engine import EventDrivenPortfolio
from agentic_quant.data_quality import (
    DataQualityError,
    MarketDataQualityService,
    inspect_market_bars,
)
from agentic_quant.coordinator import AutonomousCoordinator, COORDINATOR_STAGES
from agentic_quant.control_plane import SystemObjectStore
from agentic_quant.coordinator_runtime import (
    DOCUMENT_HISTORY_COVERAGE_EVENT,
    ResearchCoordinatorHandler,
    daily_bar_gap_windows,
    resumed_daily_history_start,
)
from agentic_quant.database import event_outbox, workflow_jobs
from agentic_quant.document_store import DocumentStore
from agentic_quant.domain import (
    BacktestCostModel,
    DataQualityStatus,
    EventEnvelope,
    PointInTimeFeatureSnapshot,
    SignalAction,
    SourceDocument,
    SourceTier,
    StockBar,
    WorkflowJob,
    WorkflowJobStatus,
)
from agentic_quant.ledger import EventLedger
from agentic_quant.market_ingestion import IngestionSummary
from agentic_quant.market_calendar import MarketSessionClock
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.providers.base import (
    DocumentFetchRequest,
    DocumentPage,
    StockBarsPage,
    StockBarsRequest,
)
from agentic_quant.reference_data import (
    GovernedReferenceImporter,
    ReferenceDataStore,
)
from agentic_quant.research_store import ResearchStore
from agentic_quant.workflow import ResumableMarketBackfill, WorkflowJobStore
from agentic_quant.ids import uuid7


def _minute_bar(minute: int, *, high: str = "101", low: str = "99") -> StockBar:
    event_time = datetime(2026, 9, 3, 14, minute, tzinfo=UTC)
    return StockBar(
        bar_id=f"bar-{minute}",
        symbol="AAPL",
        timeframe="1Min",
        event_time=event_time,
        available_from=event_time + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal("100.5"),
        volume=1000,
        trade_count=10,
        vwap=Decimal("100.2"),
        source="fixture",
        feed="sip",
        raw_object_id="TEST_RAW",
        ingested_at=datetime(2026, 9, 4, tzinfo=UTC),
    )


def _daily_bars_for_gap_test(count: int) -> tuple[StockBar, ...]:
    clock = MarketSessionClock("XNYS")
    sessions = clock.calendar.sessions_in_range("2025-01-02", "2025-06-30")[
        :count
    ]
    return tuple(
        StockBar(
            bar_id=f"daily-gap-{index}",
            symbol="AAPL",
            timeframe="1Day",
            event_time=datetime.combine(
                session.date(),
                datetime.min.time(),
                tzinfo=UTC,
            ),
            available_from=clock.daily_bar_available_from(
                datetime.combine(
                    session.date(),
                    datetime.min.time(),
                    tzinfo=UTC,
                )
            ),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=1_000_000,
            trade_count=10_000,
            vwap=Decimal("100.2"),
            source="fixture",
            feed="sip",
            raw_object_id="TEST_RAW",
            ingested_at=datetime(2026, 9, 4, tzinfo=UTC),
        )
        for index, session in enumerate(sessions)
    )


def test_data_quality_fails_closed_on_gap_and_invalid_ohlc(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    service = MarketDataQualityService(ledger.engine, ledger)
    valid = (_minute_bar(30), _minute_bar(31))
    assert service.require_bars(
        valid,
        symbol="AAPL",
        timeframe="1Min",
        code_git_sha="test-sha",
    ).status == DataQualityStatus.PASSED

    invalid = (_minute_bar(30), _minute_bar(32, high="99"))
    report = inspect_market_bars(
        invalid,
        symbol="AAPL",
        timeframe="1Min",
        code_git_sha="test-sha",
    )
    assert report.status == DataQualityStatus.FAILED
    assert report.issue_counts["missing_intervals"] == 1
    assert report.issue_counts["invalid_ohlc"] == 1
    with pytest.raises(DataQualityError, match="missing_intervals"):
        service.require_bars(
            invalid,
            symbol="AAPL",
            timeframe="1Min",
            code_git_sha="test-sha",
        )
    assert service.health_summary() == {
        "data_quality_reports": 2,
        "failed_data_quality_reports": 1,
    }


def test_data_quality_uses_exchange_calendar_for_empty_request_windows() -> None:
    weekend = inspect_market_bars(
        (),
        symbol="AAPL",
        timeframe="1Day",
        code_git_sha="test-sha",
        expected_start=datetime(2026, 9, 5, tzinfo=UTC),
        expected_end=datetime(2026, 9, 7, tzinfo=UTC),
    )
    open_session = inspect_market_bars(
        (),
        symbol="AAPL",
        timeframe="1Day",
        code_git_sha="test-sha",
        expected_start=datetime(2026, 9, 8, tzinfo=UTC),
        expected_end=datetime(2026, 9, 9, tzinfo=UTC),
    )

    assert weekend.status == DataQualityStatus.PASSED
    assert weekend.checks["expected_interval_count"] == 0
    assert open_session.status == DataQualityStatus.FAILED
    assert open_session.issue_counts["empty_dataset"] == 1
    assert open_session.issue_counts["missing_intervals"] == 1


def test_data_quality_rejects_bars_outside_the_half_open_request_window() -> None:
    report = inspect_market_bars(
        (_minute_bar(30), _minute_bar(31)),
        symbol="AAPL",
        timeframe="1Min",
        code_git_sha="test-sha",
        expected_start=datetime(2026, 9, 3, 14, 30, tzinfo=UTC),
        expected_end=datetime(2026, 9, 3, 14, 31, tzinfo=UTC),
    )

    assert report.status == DataQualityStatus.FAILED
    assert report.issue_counts["out_of_bounds"] == 1
    assert report.checks["within_requested_window"] is False


def test_half_spread_is_charged_on_both_sides() -> None:
    portfolio = EventDrivenPortfolio(
        experiment_run_id="experiment",
        symbol="AAPL",
        initial_cash=Decimal("1000"),
        cost_model=BacktestCostModel(
            commission_per_share=Decimal("0"),
            minimum_commission_per_order=Decimal("0"),
            slippage_bps_per_side=Decimal("0"),
            half_spread_bps_per_side=Decimal("5"),
            market_impact_bps_per_side=Decimal("0"),
            max_volume_participation=Decimal("1"),
        ),
    )
    snapshot = PointInTimeFeatureSnapshot(
        feature_snapshot_id="snapshot",
        evidence_packet_id="packet",
        symbol="AAPL",
        timeframe="1Min",
        as_of=datetime(2026, 9, 3, 14, 29, tzinfo=UTC),
        feature_set_version="test@0.1.0",
        values={},
        source_max_available_from=datetime(2026, 9, 3, 14, 29, tzinfo=UTC),
        data_hash="a" * 64,
        created_at=datetime(2026, 9, 3, 14, 29, tzinfo=UTC),
    )
    portfolio.record_signal(snapshot=snapshot, action=SignalAction.LONG)
    assert portfolio.enter_long(
        signal_as_of=snapshot.as_of,
        entry_time=datetime(2026, 9, 3, 14, 30, tzinfo=UTC),
        raw_price=Decimal("100"),
        available_volume=100,
        feature_snapshot_id=snapshot.feature_snapshot_id,
    )
    trade = portfolio.exit_long(
        exit_time=datetime(2026, 9, 3, 14, 31, tzinfo=UTC),
        raw_price=Decimal("100"),
        available_volume=100,
        exit_reason="test",
    )
    assert trade is not None
    assert trade.entry_price == Decimal("100.0500")
    assert trade.exit_price == Decimal("99.9500")
    fills = [event for event in portfolio.events if event.event_type.value == "fill"]
    assert all(event.details["half_spread_bps"] == "5" for event in fills)


class FakeIngestionService:
    def __init__(self) -> None:
        self.calls: list[StockBarsRequest] = []

    async def ingest_stock_bars(self, request: StockBarsRequest) -> IngestionSummary:
        self.calls.append(request)
        return IngestionSummary(
            ingestion_run_id=f"run-{len(self.calls)}",
            provider="fixture",
            symbol=request.symbol,
            pages_received=1,
            records_received=1,
            records_inserted=1,
            status="COMPLETED",
        )


def test_partitioned_backfill_resumes_without_repeating_completed_work(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    jobs = WorkflowJobStore(ledger.engine)
    fake = FakeIngestionService()
    workflow = ResumableMarketBackfill(service=fake, jobs=jobs)  # type: ignore[arg-type]
    request = StockBarsRequest(
        symbol="AAPL",
        timeframe="1Day",
        feed="sip",
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 4, tzinfo=UTC),
    )
    first = asyncio.run(
        workflow.run(request, partition_days=1, max_partitions=1)
    )
    second = asyncio.run(workflow.run(request, partition_days=1))

    assert first["status_counts"]["COMPLETED"] == 1
    assert first["status_counts"]["PENDING"] == 2
    assert second["completed"] is True
    assert len(fake.calls) == 3
    assert jobs.health_summary() == {
        "workflow_jobs": 3,
        "failed_workflow_jobs": 0,
        "exhausted_workflow_jobs": 0,
    }


def test_autonomous_coordinator_resumes_failed_stage_without_repeating_parents(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    calls: list[str] = []
    failed_once = False

    async def handler(job, dependencies):  # type: ignore[no-untyped-def]
        nonlocal failed_once
        stage = str(job.payload["stage"])
        calls.append(stage)
        if stage == "train_ml" and not failed_once:
            failed_once = True
            raise RuntimeError("transient trainer failure")
        prior = dict(dependencies[-1].result) if dependencies else {}
        return {**prior, "outcome": "COMPLETED", "last_stage": stage}

    coordinator = AutonomousCoordinator(
        WorkflowJobStore(ledger.engine),
        handler=handler,
        worker_id="test-coordinator",
    )
    cutoff = datetime(2026, 9, 5, 22, tzinfo=UTC)
    first = asyncio.run(
        coordinator.run_once(symbols=("AAPL",), as_of=cutoff)
    )
    assert first["status_counts"] == {
        "PENDING": 5,
        "RUNNING": 0,
        "COMPLETED": 3,
        "FAILED": 1,
        "EXHAUSTED": 0,
    }
    second = asyncio.run(
        coordinator.run_once(symbols=("AAPL",), as_of=cutoff)
    )
    assert second["completed"] is True
    assert calls.count("collect_market_data") == 1
    assert calls.count("materialize_features") == 1
    assert calls.count("train_ml") == 2
    assert calls[-1] == COORDINATOR_STAGES[-1]


def test_daily_gap_planner_repairs_internal_and_trailing_sessions() -> None:
    start = datetime(2026, 8, 31, tzinfo=UTC)
    end = datetime(2026, 9, 5, tzinfo=UTC)
    windows = daily_bar_gap_windows(
        start=start,
        end=end,
        existing_event_times=(
            datetime(2026, 8, 31, 4, tzinfo=UTC),
            datetime(2026, 9, 2, 4, tzinfo=UTC),
        ),
    )
    assert windows == (
        (
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 2, tzinfo=UTC),
        ),
        (
            datetime(2026, 9, 3, tzinfo=UTC),
            datetime(2026, 9, 5, tzinfo=UTC),
        ),
    )


def test_document_backfill_advances_backward_in_bounded_partitions(
    settings,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    runtime_settings = settings.model_copy(
        update={
            "alpaca_api_key": SecretStr("mock-key"),
            "alpaca_api_secret": SecretStr("mock-secret"),
            "coordinator_document_lookback_days": 365,
            "coordinator_document_partition_days": 90,
            "development_max_backfill_days": 365,
            "deployment_environment_id": "document-backfill-test",
        }
    )
    upgrade_database(runtime_settings.database_url)
    ledger = EventLedger(runtime_settings.database_url)
    requests: list[DocumentFetchRequest] = []
    as_of = datetime(2026, 9, 7, 20, tzinfo=UTC)

    class Publisher:
        def publish(self, **_payload):  # type: ignore[no-untyped-def]
            return "published"

        def health(self) -> bool:
            return True

    class EmptyNewsProvider:
        name = "alpaca_news"

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        async def fetch_documents_page(
            self,
            request: DocumentFetchRequest,
            *,
            page_token: str | None = None,
        ) -> DocumentPage:
            assert page_token is None
            requests.append(request)
            return DocumentPage(
                provider=self.name,
                data_type="news",
                provider_received_at=as_of,
                request_metadata=request.model_dump(mode="json"),
                raw_payload={"news": []},
                documents=(),
            )

    handler = ResearchCoordinatorHandler.__new__(ResearchCoordinatorHandler)
    handler.settings = runtime_settings
    handler.ledger = ledger
    handler.market = MarketDataStore(ledger.engine)
    handler.documents = DocumentStore(ledger.engine)
    handler.archive = FileRawArchive(tmp_path / "document-backfill-raw")
    handler.publisher = Publisher()
    archived = handler.archive.store_json(
        provider="alpaca_news",
        data_type="news",
        payload={"news": [{"id": "corrected-old-article"}]},
        request_metadata={"fixture": True},
        provider_received_at=as_of,
    )
    raw_object_id = handler.market.register_raw_object(archived)
    handler.documents.upsert_document(
        SourceDocument(
            document_id="00000000-0000-7000-8000-000000000099",
            provider_document_id="corrected-old-article",
            provider="alpaca_news",
            canonical_url="https://news.example/corrected-old-article",
            source_kind="news",
            source_tier=SourceTier.SECONDARY,
            publisher="Fixture Wire",
            title="An older article corrected during this window",
            summary=None,
            body_text=None,
            symbols=("AAPL",),
            published_at=as_of - timedelta(days=365),
            updated_at=as_of,
            ingested_at=as_of,
            raw_object_id="PENDING_ARCHIVE",
        ),
        raw_object_id,
    )
    context = {
        "symbol": "AAPL",
        "timeframe": "1Day",
        "as_of": as_of.isoformat(),
    }
    with patch(
        "agentic_quant.coordinator_runtime.AlpacaNewsProvider",
        return_value=EmptyNewsProvider(),
    ):
        first = asyncio.run(handler._collect_research_evidence(context))
        second = asyncio.run(handler._collect_research_evidence(context))

    assert first["outcome"] == "COMPLETED_BACKFILL_PROGRESS"
    assert second["outcome"] == "COMPLETED_BACKFILL_PROGRESS"
    assert len(requests) == 3
    assert requests[0].start == as_of - timedelta(days=90)
    assert requests[1].start == as_of - timedelta(days=180)
    assert requests[1].end == as_of - timedelta(days=90) + timedelta(seconds=1)
    assert requests[2].start == as_of - timedelta(days=1)
    assert requests[2].end == as_of
    coverage = [
        event
        for event in ledger.recent(limit=20)
        if event["event_type"] == DOCUMENT_HISTORY_COVERAGE_EVENT
    ]
    assert len(coverage) == 2
    catalog = SystemObjectStore(ledger.engine).symbol_catalog()
    apple = next(item for item in catalog["symbols"] if item["symbol"] == "AAPL")
    news = next(
        item for item in apple["datasets"] if item["key"] == "documents:news"
    )
    assert news["record_count"] == 1
    assert news["verified_window_start"] == as_of - timedelta(days=180)


def test_coordinator_treats_llm_abstention_as_advice_not_a_generation_veto() -> None:
    class Research:
        def generation_attempts(self, *, limit: int):  # type: ignore[no-untyped-def]
            assert limit == 500
            return []

    class Generator:
        async def generate(self, **payload):  # type: ignore[no-untyped-def]
            assert payload["analysis_id"] == "analysis-abstained"
            return {
                "generation_attempt_id": "attempt-1",
                "strategy_spec": {"strategy_spec_id": "strategy-1"},
            }

    handler = ResearchCoordinatorHandler.__new__(ResearchCoordinatorHandler)
    handler.research = Research()
    handler.generator = Generator()
    result = asyncio.run(
        handler._generate_strategy(
            {
                "feature_snapshot_id": "feature-1",
                "analysis_id": "analysis-abstained",
                "analysis_status": "ABSTAINED",
                "forecast_id": "forecast-1",
            }
        )
    )
    assert result["outcome"] == "COMPLETED"
    assert result["strategy_spec_id"] == "strategy-1"


def test_coordinator_retry_repairs_the_original_historical_gap(
    settings,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    runtime_settings = settings.model_copy(
        update={
            "alpaca_api_key": SecretStr("mock-key"),
            "alpaca_api_secret": SecretStr("mock-secret"),
            "coordinator_initial_lookback_days": 30,
        }
    )
    upgrade_database(runtime_settings.database_url)
    ledger = EventLedger(runtime_settings.database_url)
    market = MarketDataStore(ledger.engine)
    source = tuple(
        item.model_copy(
            update={
                "source": "alpaca",
                "feed": runtime_settings.alpaca_stock_feed,
            }
        )
        for item in _daily_bars_for_gap_test(35)
    )
    as_of = source[30].available_from + timedelta(seconds=1)
    desired_start = datetime.combine(
        (as_of - timedelta(days=30)).date(),
        datetime.min.time(),
        tzinfo=UTC,
    )
    expected = tuple(
        item for item in source if desired_start <= item.event_time < as_of
    )
    missing = expected[1]
    requests: list[StockBarsRequest] = []

    class Publisher:
        def publish(self, **payload):  # type: ignore[no-untyped-def]
            return str(len(json.loads(payload["envelope_json"])))

        def health(self) -> bool:
            return True

    class HoleOnceProvider:
        name = "alpaca"

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        async def fetch_stock_bars_page(
            self,
            request: StockBarsRequest,
            *,
            page_token: str | None = None,
        ) -> StockBarsPage:
            del page_token
            requests.append(request)
            bars = tuple(
                item
                for item in expected
                if request.start <= item.event_time < request.end
                and (len(requests) > 1 or item.bar_id != missing.bar_id)
            )
            return StockBarsPage(
                provider="alpaca",
                provider_received_at=as_of,
                request_metadata=request.model_dump(mode="json"),
                raw_payload={"fixture": True, "rows": len(bars)},
                bars=bars,
            )

    handler = ResearchCoordinatorHandler.__new__(ResearchCoordinatorHandler)
    handler.settings = runtime_settings
    handler.ledger = ledger
    handler.market = market
    handler.archive = FileRawArchive(tmp_path / "raw")
    handler.publisher = Publisher()
    provider = HoleOnceProvider()
    context = {
        "symbol": "AAPL",
        "timeframe": "1Day",
        "as_of": as_of.isoformat(),
    }
    with patch(
        "agentic_quant.coordinator_runtime.AlpacaMarketDataProvider",
        return_value=provider,
    ):
        with pytest.raises(DataQualityError, match="missing_intervals"):
            asyncio.run(handler._collect_market_data(context))
        second = asyncio.run(handler._collect_market_data(context))

    stored = market.bars_between(
        symbol="AAPL",
        timeframe="1Day",
        start=desired_start,
        end=as_of,
        source="alpaca",
        feed=runtime_settings.alpaca_stock_feed,
    )
    assert second["outcome"] == "COMPLETED"
    assert any(item.event_time == missing.event_time for item in stored)
    assert len(stored) == len(expected)
    assert requests[1].start <= missing.event_time < requests[1].end


def test_coordinator_records_and_reuses_new_listing_history_boundary(
    settings,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    runtime_settings = settings.model_copy(
        update={
            "alpaca_api_key": SecretStr("mock-key"),
            "alpaca_api_secret": SecretStr("mock-secret"),
            "coordinator_initial_lookback_days": 365,
            "development_max_backfill_days": 365,
            "deployment_environment_id": "new-listing-test",
        }
    )
    upgrade_database(runtime_settings.database_url)
    ledger = EventLedger(runtime_settings.database_url)
    market = MarketDataStore(ledger.engine)
    source = tuple(
        item.model_copy(
            update={
                "symbol": "ALAB",
                "source": "alpaca",
                "feed": runtime_settings.alpaca_stock_feed,
            }
        )
        for item in _daily_bars_for_gap_test(35)
    )
    as_of = source[-1].available_from + timedelta(seconds=1)
    requests: list[StockBarsRequest] = []

    class Publisher:
        def publish(self, **_payload):  # type: ignore[no-untyped-def]
            return "published"

        def health(self) -> bool:
            return True

    class NewlyListedProvider:
        name = "alpaca"

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        async def fetch_stock_bars_page(
            self,
            request: StockBarsRequest,
            *,
            page_token: str | None = None,
        ) -> StockBarsPage:
            del page_token
            requests.append(request)
            bars = tuple(
                item
                for item in source
                if request.start <= item.event_time < request.end
            )
            return StockBarsPage(
                provider="alpaca",
                provider_received_at=as_of,
                request_metadata=request.model_dump(mode="json"),
                raw_payload={"fixture": True, "rows": len(bars)},
                bars=bars,
            )

    handler = ResearchCoordinatorHandler.__new__(ResearchCoordinatorHandler)
    handler.settings = runtime_settings
    handler.ledger = ledger
    handler.market = market
    handler.archive = FileRawArchive(tmp_path / "new-listing-raw")
    handler.publisher = Publisher()
    provider = NewlyListedProvider()
    context = {
        "symbol": "ALAB",
        "timeframe": "1Day",
        "as_of": as_of.isoformat(),
    }
    with patch(
        "agentic_quant.coordinator_runtime.AlpacaMarketDataProvider",
        return_value=provider,
    ):
        first = asyncio.run(handler._collect_market_data(context))
        second = asyncio.run(handler._collect_market_data(context))

    assert first["outcome"] == "COMPLETED"
    assert first["history_boundary_event_id"] is not None
    assert first["verified_window_start"] == datetime.combine(
        source[0].event_time.date(),
        datetime.min.time(),
        tzinfo=UTC,
    ).isoformat()
    assert second["outcome"] == "UP_TO_DATE"
    assert second["history_boundary_event_id"] == first["history_boundary_event_id"]
    assert len(requests) == 1
    boundary_events = [
        event
        for event in ledger.recent(limit=500)
        if event["event_type"] == "market.history.boundary.observed.v1"
    ]
    assert len(boundary_events) == 1
    assert boundary_events[0]["payload"]["symbol"] == "ALAB"


def test_coordinator_uses_current_segment_after_extended_suspension(
    settings,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    runtime_settings = settings.model_copy(
        update={
            "alpaca_api_key": SecretStr("mock-key"),
            "alpaca_api_secret": SecretStr("mock-secret"),
            "coordinator_initial_lookback_days": 365,
            "development_max_backfill_days": 365,
            "deployment_environment_id": "suspension-test",
        }
    )
    upgrade_database(runtime_settings.database_url)
    ledger = EventLedger(runtime_settings.database_url)
    market = MarketDataStore(ledger.engine)
    complete = _daily_bars_for_gap_test(100)
    pre_suspension = complete[:10]
    placeholders = tuple(
        item.model_copy(
            update={"volume": 0, "trade_count": 0, "vwap": None}
        )
        for item in complete[10:35]
    )
    resumed = complete[60:]
    source = tuple(
        item.model_copy(
            update={
                "symbol": "NBIS",
                "source": "alpaca",
                "feed": runtime_settings.alpaca_stock_feed,
            }
        )
        for item in (*pre_suspension, *placeholders, *resumed)
    )
    as_of = complete[-1].available_from + timedelta(seconds=1)
    expected_start = datetime.combine(
        resumed[0].event_time.date(),
        datetime.min.time(),
        tzinfo=UTC,
    )
    assert resumed_daily_history_start(
        bars=source,
        start=complete[0].event_time,
        end=as_of,
    ) == expected_start
    requests: list[StockBarsRequest] = []

    class Publisher:
        def publish(self, **_payload):  # type: ignore[no-untyped-def]
            return "published"

        def health(self) -> bool:
            return True

    class SuspendedProvider:
        name = "alpaca"

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        async def fetch_stock_bars_page(
            self,
            request: StockBarsRequest,
            *,
            page_token: str | None = None,
        ) -> StockBarsPage:
            del page_token
            requests.append(request)
            bars = tuple(
                item
                for item in source
                if request.start <= item.event_time < request.end
            )
            return StockBarsPage(
                provider="alpaca",
                provider_received_at=as_of,
                request_metadata=request.model_dump(mode="json"),
                raw_payload={"fixture": True, "rows": len(bars)},
                bars=bars,
            )

    handler = ResearchCoordinatorHandler.__new__(ResearchCoordinatorHandler)
    handler.settings = runtime_settings
    handler.ledger = ledger
    handler.market = market
    handler.research = ResearchStore(ledger.engine)
    handler.archive = FileRawArchive(tmp_path / "suspension-raw")
    handler.publisher = Publisher()
    context = {
        "symbol": "NBIS",
        "timeframe": "1Day",
        "as_of": as_of.isoformat(),
    }
    with patch(
        "agentic_quant.coordinator_runtime.AlpacaMarketDataProvider",
        return_value=SuspendedProvider(),
    ):
        first = asyncio.run(handler._collect_market_data(context))
        second = asyncio.run(handler._collect_market_data(context))

    assert first["outcome"] == "COMPLETED"
    assert first["verified_window_start"] == expected_start.isoformat()
    assert second["outcome"] == "UP_TO_DATE"
    assert len(requests) == 1
    boundary = next(
        event
        for event in ledger.recent(limit=500)
        if event["event_id"] == first["history_boundary_event_id"]
    )
    assert boundary["payload"]["interpretation"] == (
        "PROVIDER_OBSERVED_POST_SUSPENSION_START"
    )
    verified = handler._verified_research_bars(
        context={**context, "verified_window_start": first["verified_window_start"]},
        as_of=as_of,
    )
    assert len(verified) == len(resumed)
    assert all(item.event_time >= expected_start for item in verified)


def test_autonomous_coordinator_rejects_unsupported_timeframe(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)

    async def handler(job, dependencies):  # type: ignore[no-untyped-def]
        del job, dependencies
        return {}

    coordinator = AutonomousCoordinator(
        WorkflowJobStore(EventLedger(settings.database_url).engine),
        handler=handler,
    )
    with pytest.raises(ValueError, match="only 1Day"):
        coordinator.plan(
            symbols=("AAPL",),
            as_of=datetime(2026, 9, 5, 22, tzinfo=UTC),
            timeframe="1Min",
        )


def test_autonomous_coordinator_partitions_cycles_by_research_horizon(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)

    async def handler(job, dependencies):  # type: ignore[no-untyped-def]
        del job, dependencies
        return {}

    coordinator = AutonomousCoordinator(
        WorkflowJobStore(EventLedger(settings.database_url).engine),
        handler=handler,
    )
    cutoff = datetime(2026, 9, 5, 22, tzinfo=UTC)
    daily_group, daily_jobs = coordinator.plan(
        symbols=("AAPL",),
        as_of=cutoff,
        horizon_bars=1,
    )
    annual_group, annual_jobs = coordinator.plan(
        symbols=("AAPL",),
        as_of=cutoff,
        horizon_bars=252,
    )

    assert annual_group != daily_group
    assert {job.payload["horizon_bars"] for job in daily_jobs} == {1}
    assert {job.payload["horizon_bars"] for job in annual_jobs} == {252}
    with pytest.raises(ValueError, match="horizon is not approved"):
        coordinator.plan(symbols=("AAPL",), as_of=cutoff, horizon_bars=2)


def test_research_handler_propagates_planned_horizon_into_stage_context() -> None:
    observed: dict[str, object] = {}

    async def train_ml(context):  # type: ignore[no-untyped-def]
        observed.update(context)
        return {"outcome": "COMPLETED"}

    handler = ResearchCoordinatorHandler.__new__(ResearchCoordinatorHandler)
    handler.pipeline_enabled = lambda _name: True
    handler._train_ml = train_ml
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    job = WorkflowJob(
        workflow_job_id="00000000-0000-7000-8000-000000000201",
        job_group_id="00000000-0000-7000-8000-000000000202",
        job_type="coordinator.train_ml",
        partition_key="AAPL:04:train_ml",
        request_sha256="a" * 64,
        payload={
            "symbol": "AAPL",
            "timeframe": "1Day",
            "as_of": now.isoformat(),
            "stage": "train_ml",
            "cycle_key": "2026-09-08T12",
            "horizon_bars": 20,
        },
        status=WorkflowJobStatus.PENDING,
        attempt_count=0,
        max_attempts=5,
        dependency_job_ids=(),
        cursor={},
        result={},
        created_at=now,
        updated_at=now,
    )

    result = asyncio.run(handler(job, ()))

    assert observed["horizon_bars"] == 20
    assert result["horizon_bars"] == 20


def test_autonomous_coordinator_recovers_failed_group_after_hour_rollover(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    jobs = WorkflowJobStore(ledger.engine)
    calls: list[tuple[str, str]] = []
    failed_once = False

    async def handler(job, dependencies):  # type: ignore[no-untyped-def]
        nonlocal failed_once
        stage = str(job.payload["stage"])
        calls.append((str(job.job_group_id), stage))
        if stage == "train_ml" and not failed_once:
            failed_once = True
            raise RuntimeError("one-time trainer fault")
        prior = dict(dependencies[-1].result) if dependencies else {}
        return {**prior, "outcome": "COMPLETED", "last_stage": stage}

    coordinator = AutonomousCoordinator(
        jobs,
        handler=handler,
        worker_id="hour-rollover-test",
    )
    cutoff = datetime(2026, 9, 5, 22, tzinfo=UTC)
    first = asyncio.run(
        coordinator.run_once(symbols=("AAPL",), as_of=cutoff)
    )
    old_group_id = str(first["job_group_id"])
    second = asyncio.run(
        coordinator.run_once(
            symbols=("AAPL",),
            as_of=cutoff + timedelta(hours=1),
        )
    )

    old_jobs = jobs.jobs(job_group_id=old_group_id)
    assert all(job.status.value == "COMPLETED" for job in old_jobs)
    assert next(
        job for job in old_jobs if job.payload["stage"] == "train_ml"
    ).attempt_count == 2
    assert calls.count((old_group_id, "collect_market_data")) == 1
    assert second["completed"] is True
    assert second["backlog_groups"][0]["job_group_id"] == old_group_id


def test_exhausted_groups_do_not_starve_later_retryable_work(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    jobs = WorkflowJobStore(ledger.engine)
    calls: list[str] = []

    async def handler(job, dependencies):  # type: ignore[no-untyped-def]
        del dependencies
        calls.append(str(job.workflow_job_id))
        return {"outcome": "COMPLETED"}

    coordinator = AutonomousCoordinator(jobs, handler=handler)
    now = datetime(2026, 9, 8, 20, tzinfo=UTC)
    target_id = ""
    exhausted_id = ""
    for index in range(101):
        _, planned = coordinator.plan(
            symbols=("AAPL",),
            as_of=now - timedelta(hours=102 - index),
        )
        first = next(
            item
            for item in planned
            if item.payload["stage"] == "collect_market_data"
        )
        with ledger.engine.begin() as connection:
            connection.execute(
                update(workflow_jobs)
                .where(workflow_jobs.c.workflow_job_id == first.workflow_job_id)
                .values(
                    status="FAILED",
                    attempt_count=5 if index < 100 else 1,
                )
            )
        if index == 100:
            target_id = first.workflow_job_id
        elif index == 0:
            exhausted_id = first.workflow_job_id

    preview = jobs.retry_preview(exhausted_id)
    assert preview["next_max_attempts"] == 6
    retried = jobs.retry_exhausted(
        exhausted_id,
        requested_by="test-admin",
        reason="Provider has recovered",
    )
    assert retried["status"] == "PENDING"
    result = asyncio.run(coordinator.run_once(symbols=("AAPL",), as_of=now))

    assert target_id in calls
    assert jobs.jobs_by_ids((target_id,))[0].status.value == "COMPLETED"
    assert all(
        group["job_group_id"]
        != jobs.jobs_by_ids((target_id,))[0].job_group_id
        or group["processed_this_run"] > 0
        for group in result["backlog_groups"]
    )


def test_workflow_marks_the_final_failed_attempt_exhausted(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    jobs = WorkflowJobStore(ledger.engine, ledger)

    async def handler(job, dependencies):  # type: ignore[no-untyped-def]
        del job, dependencies
        raise RuntimeError("persistent provider failure")

    coordinator = AutonomousCoordinator(jobs, handler=handler)
    cutoff = datetime(2026, 9, 8, 20, tzinfo=UTC)
    group_id, planned = coordinator.plan(
        symbols=("AAPL",),
        as_of=cutoff,
        max_attempts=1,
    )
    asyncio.run(coordinator.run_once(symbols=("AAPL",), as_of=cutoff))

    first = jobs.jobs_by_ids((planned[0].workflow_job_id,))[0]
    assert first.status.value == "EXHAUSTED"
    assert group_id not in jobs.incomplete_group_ids(
        job_type_prefix="coordinator."
    )
    assert jobs.health_summary()["exhausted_workflow_jobs"] == 1


def test_exhausted_workflow_retry_requires_admin_confirmation(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    with TestClient(create_app(settings)) as client:
        jobs = client.app.state.actions.workflow_jobs

        async def handler(job, dependencies):  # type: ignore[no-untyped-def]
            del job, dependencies
            return {"outcome": "COMPLETED"}

        coordinator = AutonomousCoordinator(jobs, handler=handler)
        _, planned = coordinator.plan(
            symbols=("AAPL",),
            as_of=datetime(2026, 9, 8, 20, tzinfo=UTC),
            max_attempts=1,
        )
        job_id = planned[0].workflow_job_id
        with jobs.engine.begin() as connection:
            connection.execute(
                update(workflow_jobs)
                .where(workflow_jobs.c.workflow_job_id == job_id)
                .values(status="EXHAUSTED", attempt_count=1)
            )
        proposed = client.post(
            "/v1/actions",
            json={
                "action_type": "workflow.retry_exhausted",
                "target_type": "workflow_job",
                "target_id": job_id,
                "parameters": {},
                "reason": "Provider incident has been resolved",
            },
        ).json()
        assert jobs.jobs_by_ids((job_id,))[0].status.value == "EXHAUSTED"
        confirmed = client.post(
            f"/v1/actions/{proposed['action_request_id']}/confirm",
            json={"confirmation_phrase": proposed["confirmation_phrase"]},
        )

        assert confirmed.status_code == 200
        retried = jobs.jobs_by_ids((job_id,))[0]
        assert retried.status.value == "PENDING"
        assert retried.max_attempts == 2


def test_fresh_running_partition_is_not_stolen_by_another_invoker(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    jobs = WorkflowJobStore(ledger.engine)
    workflow = ResumableMarketBackfill(  # type: ignore[arg-type]
        service=FakeIngestionService(),
        jobs=jobs,
    )
    job_group_id, planned = workflow.plan(
        StockBarsRequest(
            symbol="AAPL",
            timeframe="1Day",
            feed="sip",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        ),
        partition_days=1,
    )

    lease_token = jobs.claim(planned[0].workflow_job_id, worker_id="inline-worker")
    assert lease_token is not None
    assert jobs.requeue_stale(job_group_id=job_group_id) == 0
    assert jobs.claim(planned[0].workflow_job_id) is None
    assert jobs.heartbeat(
        planned[0].workflow_job_id,
        worker_id="different-worker",
        lease_token=lease_token,
    ) is False
    assert jobs.heartbeat(
        planned[0].workflow_job_id,
        worker_id="inline-worker",
        lease_token=lease_token,
    ) is True
    with pytest.raises(ValueError, match="active lease owner"):
        jobs.complete(
            planned[0].workflow_job_id,
            result={"ok": True},
            worker_id="different-worker",
            lease_token=lease_token,
        )
    jobs.complete(
        planned[0].workflow_job_id,
        result={"ok": True},
        worker_id="inline-worker",
        lease_token=lease_token,
    )
    completed = jobs.jobs(job_group_id=job_group_id)[0]
    assert completed.status.value == "COMPLETED"
    assert completed.lease_owner is None


def test_reclaimed_workflow_attempt_rejects_stale_same_owner_token(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    jobs = WorkflowJobStore(ledger.engine)
    workflow = ResumableMarketBackfill(
        service=FakeIngestionService(), jobs=jobs  # type: ignore[arg-type]
    )
    group_id, planned = workflow.plan(
        StockBarsRequest(
            symbol="AAPL",
            timeframe="1Day",
            feed="sip",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        ),
        partition_days=1,
    )
    first_token = jobs.claim(
        planned[0].workflow_job_id,
        worker_id="same-process",
        lease_for=timedelta(microseconds=1),
    )
    assert first_token is not None
    with ledger.engine.begin() as connection:
        connection.execute(
            update(workflow_jobs)
            .where(
                workflow_jobs.c.workflow_job_id == planned[0].workflow_job_id
            )
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert jobs.heartbeat(
        planned[0].workflow_job_id,
        worker_id="same-process",
        lease_token=first_token,
    ) is False
    assert jobs.requeue_stale(job_group_id=group_id) == 1
    second_token = jobs.claim(
        planned[0].workflow_job_id,
        worker_id="same-process",
    )
    assert second_token is not None and second_token != first_token
    with pytest.raises(ValueError, match="active lease owner"):
        jobs.complete(
            planned[0].workflow_job_id,
            result={"attempt": 1},
            worker_id="same-process",
            lease_token=first_token,
        )
    jobs.complete(
        planned[0].workflow_job_id,
        result={"attempt": 2},
        worker_id="same-process",
        lease_token=second_token,
    )


def test_backfill_range_extension_reuses_completed_fixed_partitions(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    jobs = WorkflowJobStore(ledger.engine)
    workflow = ResumableMarketBackfill(
        service=FakeIngestionService(), jobs=jobs  # type: ignore[arg-type]
    )
    short = StockBarsRequest(
        symbol="AAPL",
        timeframe="1Day",
        feed="sip",
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 3, tzinfo=UTC),
    )
    extended = short.model_copy(
        update={"end": datetime(2026, 1, 4, tzinfo=UTC)}
    )
    first_group, first_jobs = workflow.plan(short, partition_days=1)
    second_group, extended_jobs = workflow.plan(extended, partition_days=1)
    assert first_group == second_group
    assert [job.workflow_job_id for job in extended_jobs[:2]] == [
        job.workflow_job_id for job in first_jobs
    ]
    assert len(extended_jobs) == 3


def test_event_outbox_retries_with_stable_event_identity(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    event = EventEnvelope(
        event_id=uuid7(),
        event_type="fixture.created.v1",
        event_time=datetime.now(UTC),
        emitted_at=datetime.now(UTC),
        producer="test",
        correlation_id=uuid7(),
        payload={"value": 1},
    )
    assert ledger.append(event) is True

    class FlakyPublisher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def publish(self, *, event_type: str, event_id: str, envelope_json: str) -> str:
            self.calls.append(event_id)
            if len(self.calls) == 1:
                raise RuntimeError("temporary event bus failure")
            assert event_type == event.event_type
            assert event.event_id in envelope_json
            return "stream-id"

    publisher = FlakyPublisher()
    first = ledger.publish_pending(publisher, worker_id="worker-a")
    assert first == {"published": 0, "failed": 1}
    assert ledger.outbox_health()["event_outbox_failed"] == 1
    with ledger.engine.begin() as connection:
        connection.execute(
            update(event_outbox)
            .where(event_outbox.c.event_id == event.event_id)
            .values(next_attempt_at=datetime.now(UTC))
        )
    second = ledger.publish_pending(publisher, worker_id="worker-b")
    assert second == {"published": 1, "failed": 0}
    assert publisher.calls == [event.event_id, event.event_id]
    assert ledger.outbox_health()["event_outbox_published"] == 1


def test_dead_outbox_event_requires_explicit_requeue(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    event = EventEnvelope(
        event_id=uuid7(),
        event_type="fixture.dead.v1",
        event_time=datetime.now(UTC),
        emitted_at=datetime.now(UTC),
        producer="test",
        correlation_id=uuid7(),
        payload={"value": 1},
    )
    ledger.append(event)
    with ledger.engine.begin() as connection:
        connection.execute(
            update(event_outbox)
            .where(event_outbox.c.event_id == event.event_id)
            .values(
                status="DEAD",
                attempt_count=8,
                last_error="RuntimeError",
            )
        )

    dead = ledger.outbox_entries(status="DEAD")
    assert [item["event_id"] for item in dead] == [event.event_id]
    requeued = ledger.requeue_dead(event.event_id)
    assert requeued["status"] == "PENDING"
    assert requeued["attempt_count"] == 0
    assert requeued["last_error"] is None
    with pytest.raises(ValueError, match="Only a dead"):
        ledger.requeue_dead(event.event_id)


def test_governed_reference_import_is_idempotent(settings) -> None:  # type: ignore[no-untyped-def]
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    store = ReferenceDataStore(ledger.engine)
    importer = GovernedReferenceImporter(store, ledger)
    payload = {
        "dataset_type": "corporate_actions",
        "source": "governed-fixture",
        "source_version": "fixture@1",
        "records": [
            {
                "symbol": "AAPL",
                "action_type": "split",
                "effective_at": "2025-06-01T00:00:00+00:00",
                "available_from": "2025-05-15T00:00:00+00:00",
                "split_ratio": "2",
            }
        ],
    }
    first = importer.import_payload(payload)
    replay = importer.import_payload(payload)
    assert first.records_inserted == 1
    assert replay.reference_import_id == first.reference_import_id
    assert replay.records_inserted == 0
    assert store.health_summary()["corporate_actions"] == 1
    assert store.health_summary()["reference_imports"] == 1
    events = ledger.by_correlation_id(first.reference_import_id)
    assert events[-1]["event_type"] == "reference_data.imported.v1"
