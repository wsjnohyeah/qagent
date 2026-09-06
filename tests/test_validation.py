from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from agentic_quant.api import create_app
from agentic_quant.domain import BacktestCostModel, StockBar
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.market_calendar import MarketSessionClock
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.research_store import ResearchStore
from agentic_quant.research import default_strategy_spec, research_code_sha256
from agentic_quant.validation import (
    PromotionGatePolicy,
    WalkForwardValidator,
    assess_research_gate,
    continuous_oos_equity_and_drawdown,
    combinatorial_purged_diagnostics,
    deflated_sharpe_diagnostics,
)


def test_continuous_oos_drawdown_keeps_intrafold_loss_and_cross_fold_peak() -> None:
    path, drawdown = continuous_oos_equity_and_drawdown(
        (
            (Decimal("100"), Decimal("50"), Decimal("110")),
            (Decimal("100"), Decimal("90")),
        )
    )
    assert path == (
        Decimal("1"),
        Decimal("0.5"),
        Decimal("1.1"),
        Decimal("0.99"),
    )
    assert drawdown == Decimal("-0.5")


def _regime_bars(count: int = 60) -> tuple[StockBar, ...]:
    clock = MarketSessionClock("XNYS")
    sessions = clock.calendar.sessions_in_range("2025-01-02", "2025-06-30")[:count]
    price = Decimal("100")
    bars: list[StockBar] = []
    for index, session in enumerate(sessions):
        event_time = datetime.combine(
            session.date(),
            datetime.min.time(),
            tzinfo=UTC,
        )
        change = Decimal("0.01") if index < 38 else Decimal("-0.012")
        close = price * (Decimal("1") + change)
        bars.append(
            StockBar(
                bar_id=uuid7(),
                symbol="AAPL",
                timeframe="1Day",
                event_time=event_time,
                available_from=clock.daily_bar_available_from(event_time),
                open=price,
                high=max(price, close) * Decimal("1.002"),
                low=min(price, close) * Decimal("0.998"),
                close=close,
                volume=1_000_000,
                trade_count=10_000,
                vwap=(price + close) / Decimal("2"),
                source="fixture",
                feed="test",
                raw_object_id="TEST_RAW",
                ingested_at=datetime(2026, 9, 4, tzinfo=UTC),
            )
        )
        price = close
    return tuple(bars)


def _validator(settings):  # type: ignore[no-untyped-def]
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    market_store = MarketDataStore(ledger.engine)
    research_store = ResearchStore(ledger.engine)
    bars = _regime_bars()
    market_store.insert_bars(bars, raw_object_id="TEST_RAW")
    return ledger, research_store, WalkForwardValidator(research_store, ledger), bars


def test_walk_forward_validation_preserves_embargo_and_all_candidates(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    ledger, store, validator, bars = _validator(settings)
    report = validator.run(
        symbol="AAPL",
        timeframe="1Day",
        as_of_start=bars[20].available_from,
        as_of_end=bars[-1].available_from,
        code_git_sha="test-git-sha",
        strategy_types=("buy_and_hold", "momentum"),
        train_bars=22,
        test_bars=5,
        step_bars=5,
        embargo_bars=1,
        cost_model=BacktestCostModel(
            commission_per_share=Decimal("0"),
            minimum_commission_per_order=Decimal("0"),
            slippage_bps_per_side=Decimal("0"),
            half_spread_bps_per_side=Decimal("0"),
            market_impact_bps_per_side=Decimal("0"),
        ),
    )

    assert len(report.folds) == 3
    assert all(fold.train_end < fold.test_start for fold in report.folds)
    assert all(
        set(fold.train_experiment_ids) == {"buy_and_hold", "momentum"}
        and set(fold.test_experiment_ids) == {"buy_and_hold", "momentum"}
        for fold in report.folds
    )
    assert all(fold.selected_test_rank in {1, 2} for fold in report.folds)
    assert report.aggregate_metrics["fold_count"] == 3
    pbo = report.robustness_metrics["combinatorial_purged_validation"]
    assert pbo["group_count"] == 3
    assert pbo["combination_count_evaluated"] == 3
    assert Decimal("0") <= pbo["probability_of_backtest_overfitting"] <= Decimal(
        "1"
    )
    assert report.robustness_metrics["deflated_sharpe"]["sample_size"] == 3
    assert report.gate_assessment["status"] == "INSUFFICIENT_EVIDENCE"
    assert report.gate_assessment["automatic_promotion"] is False
    assert len(report.report_hash) == 64
    assert store.health_summary()["experiment_runs"] == 12
    assert store.health_summary()["validation_reports"] == 1
    assert store.health_summary()["validation_folds"] == 3
    recent = store.recent_validation_reports(limit=1)
    assert recent[0]["validation_report_id"] == report.validation_report_id
    assert recent[0]["fold_count"] == 3
    stored = store.validation_report(report.validation_report_id)
    assert stored is not None
    assert stored["gate_assessment"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert [fold["fold_number"] for fold in stored["folds"]] == [1, 2, 3]
    events = ledger.by_correlation_id(report.validation_report_id)
    assert events[-1]["event_type"] == "research.validation.completed.v1"


def test_walk_forward_validation_rejects_overlapping_test_windows(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    _, _, validator, bars = _validator(settings)
    with pytest.raises(ValueError, match="step_bars must be at least test_bars"):
        validator.run(
            symbol="AAPL",
            timeframe="1Day",
            as_of_start=bars[20].available_from,
            as_of_end=bars[-1].available_from,
            code_git_sha="test-git-sha",
            train_bars=22,
            test_bars=5,
            step_bars=4,
            embargo_bars=1,
        )


def test_api_validates_the_exact_generated_strategy_spec_id(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    _, store, _, bars = _validator(settings)
    spec = store.record_strategy_spec(
        default_strategy_spec(
            "momentum",
            timeframe="1Day",
            code_sha256=research_code_sha256(),
        )
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/research/validations",
            json={
                "strategy_spec_id": spec.strategy_spec_id,
                "symbol": "AAPL",
                "timeframe": "1Day",
                "as_of_start": bars[20].available_from.isoformat(),
                "as_of_end": bars[-1].available_from.isoformat(),
                "train_bars": 22,
                "test_bars": 5,
                "step_bars": 5,
                "embargo_bars": 1,
                "initial_equity": "100000",
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["validation_subject"] == "static_strategy"
    assert payload["validated_strategy_spec_ids"] == {
        "momentum": spec.strategy_spec_id
    }
    assert payload["gate_assessment"]["status"] == "INSUFFICIENT_EVIDENCE"


def test_pbo_and_deflated_sharpe_diagnostics_are_bounded_and_deterministic() -> None:
    scores = {
        "alpha": (
            Decimal("1.0"),
            Decimal("0.8"),
            Decimal("-0.2"),
            Decimal("0.7"),
        ),
        "baseline": (
            Decimal("0.2"),
            Decimal("0.3"),
            Decimal("0.1"),
            Decimal("0.2"),
        ),
        "unstable": (
            Decimal("2.0"),
            Decimal("-2.0"),
            Decimal("2.0"),
            Decimal("-2.0"),
        ),
    }
    first = combinatorial_purged_diagnostics(scores)
    second = combinatorial_purged_diagnostics(scores)
    assert first == second
    assert first["combination_count_total"] == 6
    assert Decimal("0") <= first["probability_of_backtest_overfitting"] <= Decimal(
        "1"
    )

    positive = deflated_sharpe_diagnostics(
        tuple(
            Decimal(value)
            for value in (
                "0.01",
                "0.02",
                "0.015",
                "0.03",
                "0.012",
                "0.018",
                "0.025",
                "0.011",
                "0.019",
                "0.017",
                "0.022",
                "0.014",
            )
        ),
        number_of_trials=3,
    )
    negative = deflated_sharpe_diagnostics(
        tuple(-value for value in scores["baseline"]),
        number_of_trials=3,
    )
    assert positive["deflated_sharpe_probability"] > negative[
        "deflated_sharpe_probability"
    ]


def test_research_gate_never_auto_promotes_and_requires_enough_evidence() -> None:
    policy = PromotionGatePolicy(
        version="research_gate@0.1.0",
        minimum_oos_folds=4,
        minimum_candidate_count=2,
        minimum_regime_count=2,
        maximum_probability_of_backtest_overfitting=Decimal("0.25"),
        minimum_deflated_sharpe_probability=Decimal("0.90"),
        minimum_positive_oos_fold_rate=Decimal("0.50"),
        maximum_allowed_drawdown=Decimal("-0.20"),
    )
    eligible = assess_research_gate(
        policy=policy,
        fold_count=8,
        candidate_count=3,
        regime_count=3,
        positive_fold_rate=Decimal("0.75"),
        worst_drawdown=Decimal("-0.10"),
        probability_of_backtest_overfitting=Decimal("0.10"),
        deflated_sharpe_probability=Decimal("0.97"),
    )
    insufficient = assess_research_gate(
        policy=policy,
        fold_count=2,
        candidate_count=3,
        regime_count=1,
        positive_fold_rate=Decimal("0.75"),
        worst_drawdown=Decimal("-0.10"),
        probability_of_backtest_overfitting=Decimal("0.10"),
        deflated_sharpe_probability=Decimal("0.97"),
    )
    assert eligible["status"] == "ELIGIBLE_FOR_HUMAN_REVIEW"
    assert eligible["automatic_promotion"] is False
    assert insufficient["status"] == "INSUFFICIENT_EVIDENCE"
