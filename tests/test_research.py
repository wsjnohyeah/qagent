from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import insert, select

from agentic_quant.database import catalysts, evidence_packets, feature_snapshots
from agentic_quant.domain import BacktestCostModel, StockBar
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.research import (
    PointInTimeFeatureBuilder,
    ResearchBacktester,
    default_strategy_spec,
    research_code_sha256,
)
from agentic_quant.research_store import ResearchStore


def _daily_bars(symbol: str = "AAPL", count: int = 80) -> tuple[StockBar, ...]:
    start = datetime(2025, 1, 2, 5, tzinfo=UTC)
    bars: list[StockBar] = []
    price = Decimal("100")
    for index in range(count):
        event_time = start + timedelta(days=index)
        change = Decimal("-0.025") if index % 13 == 0 else Decimal("0.006")
        close = price * (Decimal("1") + change)
        bars.append(
            StockBar(
                bar_id=uuid7(),
                symbol=symbol,
                timeframe="1Day",
                event_time=event_time,
                available_from=event_time + timedelta(days=1),
                open=price,
                high=max(price, close) * Decimal("1.002"),
                low=min(price, close) * Decimal("0.998"),
                close=close,
                volume=1_000_000 + index * 1_000,
                trade_count=10_000 + index,
                vwap=(price + close) / Decimal("2"),
                source="synthetic",
                feed="test",
                raw_object_id="TEST_RAW",
                ingested_at=datetime(2026, 9, 4, tzinfo=UTC),
            )
        )
        price = close
    return tuple(bars)


def _stores(settings):  # type: ignore[no-untyped-def]
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    return ledger, MarketDataStore(ledger.engine), ResearchStore(ledger.engine)


def test_feature_snapshot_excludes_future_evidence_and_is_idempotent(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    ledger, market_store, research_store = _stores(settings)
    bars = _daily_bars(count=25)
    market_store.insert_bars(bars, raw_object_id="TEST_RAW")
    as_of = bars[20].available_from
    with ledger.engine.begin() as connection:
        connection.execute(
            insert(catalysts),
            [
                {
                    "catalyst_id": uuid7(),
                    "canonical_key": "past-catalyst",
                    "catalyst_type": "earnings",
                    "primary_symbol": "AAPL",
                    "event_time": as_of - timedelta(days=2),
                    "available_from": as_of - timedelta(days=1),
                    "last_updated_at": as_of - timedelta(days=1),
                    "headline": "Past evidence",
                    "primary_source_document_id": None,
                    "status": "ACTIVE",
                    "source_count": 1,
                },
                {
                    "catalyst_id": uuid7(),
                    "canonical_key": "future-availability",
                    "catalyst_type": "earnings",
                    "primary_symbol": "AAPL",
                    "event_time": as_of - timedelta(hours=1),
                    "available_from": as_of + timedelta(hours=1),
                    "last_updated_at": as_of + timedelta(hours=1),
                    "headline": "Not available yet",
                    "primary_source_document_id": None,
                    "status": "ACTIVE",
                    "source_count": 1,
                },
            ],
        )
    builder = PointInTimeFeatureBuilder(research_store)
    first = builder.build(
        symbol="AAPL",
        timeframe="1Day",
        as_of=as_of,
        bars=bars,
    )
    second = builder.build(
        symbol="AAPL",
        timeframe="1Day",
        as_of=as_of,
        bars=bars,
    )
    assert first.feature_snapshot_id == second.feature_snapshot_id
    assert first.source_max_available_from <= first.as_of
    assert first.values["bar_count"] == 21
    assert first.values["catalyst_count_90d"] == 1
    with ledger.engine.connect() as connection:
        packet = connection.execute(select(evidence_packets)).one()
        references = packet.references_json
        assert all(
            datetime.fromisoformat(item["available_from"].replace("Z", "+00:00")) <= as_of
            for item in references
        )
        assert len(connection.execute(select(feature_snapshots)).all()) == 1


def test_cost_aware_backtest_uses_next_bar_and_records_immutable_run(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    ledger, market_store, research_store = _stores(settings)
    bars = _daily_bars(count=80)
    market_store.insert_bars(bars, raw_object_id="TEST_RAW")
    spec = default_strategy_spec(
        "momentum",
        timeframe="1Day",
        code_sha256=research_code_sha256(),
    )
    stored_spec = research_store.record_strategy_spec(spec)
    replayed_spec = research_store.record_strategy_spec(
        spec.model_copy(update={"strategy_spec_id": uuid7()})
    )
    assert replayed_spec.strategy_spec_id == stored_spec.strategy_spec_id
    result = ResearchBacktester(research_store, ledger).run(
        spec=spec,
        symbol="AAPL",
        as_of_start=bars[20].available_from,
        as_of_end=bars[-1].available_from,
        code_git_sha="test-git-sha",
        initial_equity=Decimal("100000"),
        cost_model=BacktestCostModel(
            commission_per_share=Decimal("0.01"),
            minimum_commission_per_order=Decimal("1"),
            slippage_bps_per_side=Decimal("5"),
        ),
    )
    assert result.experiment.metrics.trade_count > 0
    assert result.experiment.metrics.total_cost > 0
    assert result.experiment.metrics.sortino_ratio < Decimal("100")
    assert all(trade.entry_time >= trade.signal_as_of for trade in result.trades)
    assert result.experiment.dataset_hash
    assert research_store.health_summary()["experiment_runs"] == 1
    assert research_store.health_summary()["backtest_trades"] == len(result.trades)
    recent = research_store.recent_experiments(limit=1)
    assert recent[0]["feature_snapshot_count"] == len(
        result.experiment.feature_snapshot_ids
    )
    assert "feature_snapshot_ids" not in recent[0]
    events = ledger.by_correlation_id(result.experiment.experiment_run_id)
    assert events[-1]["event_type"] == "research.experiment.completed.v1"


def test_research_store_refuses_future_market_bar(settings) -> None:  # type: ignore[no-untyped-def]
    _, _, research_store = _stores(settings)
    bars = _daily_bars(count=21)
    as_of = bars[-1].event_time
    builder = PointInTimeFeatureBuilder(research_store)
    try:
        builder.build(
            symbol="AAPL",
            timeframe="1Day",
            as_of=as_of,
            bars=bars,
        )
    except ValueError as exc:
        assert "at least 21 available bars" in str(exc)
    else:
        raise AssertionError("Future-unavailable bars must not enter a feature snapshot")
