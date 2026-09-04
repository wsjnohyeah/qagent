from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agentic_quant.backtest_engine import EventDrivenPortfolio
from agentic_quant.data_quality import (
    DataQualityError,
    MarketDataQualityService,
    inspect_market_bars,
)
from agentic_quant.domain import (
    BacktestCostModel,
    DataQualityStatus,
    PointInTimeFeatureSnapshot,
    SignalAction,
    StockBar,
)
from agentic_quant.ledger import EventLedger
from agentic_quant.market_ingestion import IngestionSummary
from agentic_quant.migrations import upgrade_database
from agentic_quant.providers.base import StockBarsRequest
from agentic_quant.reference_data import (
    GovernedReferenceImporter,
    ReferenceDataStore,
)
from agentic_quant.workflow import ResumableMarketBackfill, WorkflowJobStore


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
    }


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

    assert jobs.claim(planned[0].workflow_job_id) is True
    assert jobs.requeue_stale(job_group_id=job_group_id) == 0
    assert jobs.claim(planned[0].workflow_job_id) is False


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
