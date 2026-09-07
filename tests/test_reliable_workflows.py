from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update

from agentic_quant.backtest_engine import EventDrivenPortfolio
from agentic_quant.data_quality import (
    DataQualityError,
    MarketDataQualityService,
    inspect_market_bars,
)
from agentic_quant.coordinator import AutonomousCoordinator, COORDINATOR_STAGES
from agentic_quant.coordinator_runtime import daily_bar_gap_windows
from agentic_quant.database import event_outbox, workflow_jobs
from agentic_quant.domain import (
    BacktestCostModel,
    DataQualityStatus,
    EventEnvelope,
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
