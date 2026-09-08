from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from agentic_quant.api import create_app
from agentic_quant.coordinator_runtime import ResearchCoordinatorHandler
from agentic_quant.control_plane import SystemObjectStore
from agentic_quant.data_quality import DataQualityError
from agentic_quant.database import shadow_trade_plans, validation_reports, workflow_jobs
from agentic_quant.domain import BacktestCostModel, EventEnvelope, StockBar
from agentic_quant.ids import uuid7
from agentic_quant.ledger import EventLedger
from agentic_quant.market_calendar import MarketSessionClock
from agentic_quant.market_scanner import MARKET_SCAN_EVENT
from agentic_quant.market_store import MarketDataStore
from agentic_quant.migrations import upgrade_database
from agentic_quant.research_store import ResearchStore, _canonical_hash
from agentic_quant.research import (
    BACKTEST_ENGINE_VERSION,
    PointInTimeFeatureBuilder,
    default_strategy_spec,
    research_code_sha256,
)
from agentic_quant.validation import (
    DEFAULT_PROMOTION_GATE_POLICY,
    PromotionGatePolicy,
    WalkForwardValidator,
    assess_research_gate,
    continuous_oos_equity_and_drawdown,
    combinatorial_purged_diagnostics,
    deflated_sharpe_diagnostics,
    promotion_policy_sha256,
    validation_execution_contract,
    validation_input_fingerprint,
)
from agentic_quant.risk import RestrictionRegistry, RiskPolicy
from agentic_quant.shadow import ShadowRuntime


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


def _seed_eligible_report(
    ledger: EventLedger,
    *,
    spec,  # type: ignore[no-untyped-def]
    timeframe: str,
    risk_policy: RiskPolicy,
    restrictions: RestrictionRegistry,
    symbol: str = "AAPL",
) -> str:
    report_id = uuid7()
    contract = validation_execution_contract(
        validation_subject="static_strategy",
        validated_strategy_spec_ids={
            str(spec.strategy_type): spec.strategy_spec_id
        },
        cost_model=BacktestCostModel(),
        risk_policy=risk_policy,
        restriction_registry_version=restrictions.version,
        initial_equity=Decimal("100000"),
        strategy_spec=spec,
    )
    with ledger.engine.begin() as connection:
        connection.execute(
            insert(validation_reports).values(
                validation_report_id=report_id,
                symbol=symbol,
                timeframe=timeframe,
                strategy_types=[str(spec.strategy_type)],
                validation_subject="static_strategy",
                validated_strategy_spec_ids={
                    str(spec.strategy_type): spec.strategy_spec_id
                },
                execution_contract_json=contract,
                execution_contract_sha256=_canonical_hash(contract),
                selection_metric="sharpe_ratio",
                train_bars=22,
                test_bars=5,
                step_bars=5,
                embargo_bars=1,
                aggregate_metrics={},
                regime_metrics={},
                robustness_metrics={"selection_search_trial_count": 1},
                gate_assessment={
                    "eligible_for_human_review": True,
                    "policy_sha256": promotion_policy_sha256(
                        DEFAULT_PROMOTION_GATE_POLICY
                    ),
                },
                report_hash="a" * 64,
                code_git_sha="test-fixture-only",
                created_at=datetime.now(UTC),
            )
        )
    return report_id


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
    assert report.robustness_metrics["deflated_sharpe"]["number_of_trials"] == 2
    assert report.robustness_metrics["selection_search_trial_count"] == 2
    validation_input = report.robustness_metrics["validation_input"]
    assert validation_input["bar_count"] == len(bars)
    assert len(validation_input["market_data_sha256"]) == 64
    changed_bars = (
        bars[0].model_copy(update={"close": bars[0].close + Decimal("0.01")}),
        *bars[1:],
    )
    changed_input = validation_input_fingerprint(
        bars=changed_bars,
        as_of_start=bars[20].available_from,
        selection_metric="sharpe_ratio",
        train_bars=22,
        test_bars=5,
        step_bars=5,
        embargo_bars=1,
    )
    assert changed_input["market_data_sha256"] != validation_input[
        "market_data_sha256"
    ]
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


def test_real_static_validation_can_reach_adoption_and_shadow_start(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    market = MarketDataStore(ledger.engine)
    store = ResearchStore(ledger.engine)
    bars = _regime_bars(count=75)
    market.insert_bars(bars, raw_object_id="TEST_RAW")
    spec = store.record_strategy_spec(
        default_strategy_spec(
            "momentum",
            timeframe="1Day",
            code_sha256=research_code_sha256(),
        )
    )
    permissive_test_policy = PromotionGatePolicy(
        version="research_gate@0.2.0",
        minimum_oos_folds=4,
        minimum_active_oos_folds=0,
        minimum_oos_trades=0,
        minimum_candidate_count=3,
        minimum_regime_count=1,
        maximum_probability_of_backtest_overfitting=Decimal("0.20"),
        minimum_deflated_sharpe_probability=Decimal("0"),
        minimum_positive_active_oos_fold_rate=Decimal("0"),
        maximum_allowed_drawdown=Decimal("-1"),
        candidate_shadow_minimum_oos_folds=1,
        candidate_shadow_minimum_active_oos_folds=1,
        candidate_shadow_minimum_oos_trades=1,
        candidate_shadow_minimum_compounded_oos_return=Decimal("-1"),
        candidate_shadow_maximum_allowed_drawdown=Decimal("-1"),
    )
    validator = WalkForwardValidator(
        store,
        ledger,
        promotion_policy=permissive_test_policy,
        risk_policy=RiskPolicy.from_yaml(settings.risk_policy_path),
        restrictions=RestrictionRegistry.from_yaml(
            settings.restricted_securities_path
        ),
    )
    report = validator.run(
        symbol="AAPL",
        timeframe="1Day",
        as_of_start=bars[20].available_from,
        as_of_end=bars[-1].available_from,
        code_git_sha="test-git-sha",
        strategy_spec=spec,
        train_bars=22,
        test_bars=5,
        step_bars=5,
        embargo_bars=1,
    )
    assert report.gate_assessment["status"] == "ELIGIBLE_FOR_HUMAN_REVIEW"
    objects = SystemObjectStore(ledger.engine, ledger)
    objects.ensure_defaults()
    shadow = ShadowRuntime(
        ledger.engine,
        store,
        objects,
        risk_policy=RiskPolicy.from_yaml(settings.risk_policy_path),
        restrictions=RestrictionRegistry.from_yaml(
            settings.restricted_securities_path
        ),
        promotion_policy=permissive_test_policy,
    )
    shadow.initialize_virtual_account()
    adoption = shadow.adopt_strategy(
        strategy_spec_id=spec.strategy_spec_id,
        validation_report_id=report.validation_report_id,
        reason="Tested real exact-spec validation path",
        approved_by="test-admin",
    )
    deployment = shadow.start_deployment(
        strategy_spec_id=spec.strategy_spec_id,
        symbol="AAPL",
        initial_cash=Decimal("100000"),
        requested_by="test-admin",
    )
    assert adoption["status"] == "ADOPTED_FOR_SHADOW"
    assert adoption["admission_tier"] == "QUALIFIED"
    assert deployment["status"] == "ACTIVE"

    snapshot = PointInTimeFeatureBuilder(store).build(
        symbol="AAPL",
        timeframe="1Day",
        as_of=bars[-1].available_from,
        bars=bars,
    )
    for index in range(2):
        later_attempt_id = uuid7()
        store.create_generation_attempt(
            generation_attempt_id=later_attempt_id,
            feature_snapshot_id=snapshot.feature_snapshot_id,
            analysis_id=f"later-research-analysis-{index}",
            forecast_id=f"later-research-forecast-{index}",
            provider=None,
        )
        store.update_generation_attempt(
            later_attempt_id,
            status="ACCEPT",
            strategy_spec_id=spec.strategy_spec_id,
        )
    with pytest.raises(ValueError, match="research search count"):
        shadow.adoption_preview(
            strategy_spec_id=spec.strategy_spec_id,
            validation_report_id=report.validation_report_id,
        )
    assert shadow.deployments()[0]["contract_status"] == "CURRENT"


def test_scanner_pool_lineage_can_authorize_confirmed_shadow_start(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    store = ResearchStore(ledger.engine)
    spec = store.record_strategy_spec(
        default_strategy_spec(
            "momentum",
            timeframe="1Day",
            code_sha256=research_code_sha256(),
        ).model_copy(
            update={
                "strategy_spec_id": uuid7(),
                "name": "scanner_pool_momentum",
                "version": "scanner-pool-test@0.1.0",
            }
        )
    )
    policy = RiskPolicy.from_yaml(settings.risk_policy_path)
    restrictions = RestrictionRegistry.from_yaml(
        settings.restricted_securities_path
    )
    report_id = _seed_eligible_report(
        ledger,
        spec=spec,
        timeframe="1Day",
        risk_policy=policy,
        restrictions=restrictions,
        symbol="SNDK",
    )
    objects = SystemObjectStore(ledger.engine, ledger)
    objects.ensure_defaults()
    pool = objects.replace_list_members(
        slug_or_id="scanner-trading-pool",
        members=["SNDK"],
        reason="LLM-reviewed scanner fixture",
        created_by="market-universe-scanner",
    )
    scan_id = uuid7()
    now = datetime.now(UTC)
    ledger.append(
        EventEnvelope(
            event_id=uuid7(),
            event_type=MARKET_SCAN_EVENT,
            event_time=now,
            emitted_at=now,
            producer="market-universe-scanner",
            correlation_id=scan_id,
            payload={
                "trading_pool_admission": {
                    "status": "UPDATED",
                    "list_revision": pool["current_revision"],
                    "admitted_symbols": ["SNDK"],
                    "basis_scan_id": scan_id,
                    "basis_llm_invocation_id": "llm-scan-fixture",
                }
            },
        )
    )
    with ledger.engine.begin() as connection:
        connection.execute(
            insert(workflow_jobs).values(
                workflow_job_id=uuid7(),
                job_group_id=uuid7(),
                job_type="coordinator.validate_strategy",
                partition_key="SNDK:08:validate_strategy",
                request_sha256="b" * 64,
                payload_json={
                    "symbol": "SNDK",
                    "timeframe": "1Day",
                    "universe_scan_id": scan_id,
                },
                status="COMPLETED",
                attempt_count=1,
                max_attempts=5,
                dependency_job_ids_json=[],
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                cursor_json={},
                result_json={"validation_report_id": report_id},
                error_code=None,
                created_at=now,
                started_at=now,
                updated_at=now,
                completed_at=now,
            )
        )
    shadow = ShadowRuntime(
        ledger.engine,
        store,
        objects,
        risk_policy=policy,
        restrictions=restrictions,
    )
    shadow.initialize_virtual_account()

    adoption = shadow.adopt_strategy(
        strategy_spec_id=spec.strategy_spec_id,
        validation_report_id=report_id,
        reason="Reviewed scanner-pool strategy",
        approved_by="test-admin",
    )
    preview = shadow.deployment_preview(
        strategy_spec_id=spec.strategy_spec_id,
        symbol="SNDK",
        initial_cash=Decimal("100000"),
    )

    assert adoption["status"] == "ADOPTED_FOR_SHADOW"
    assert preview["universe_admission"]["authority"] == (
        "SCANNER_LLM_TRADING_POOL"
    )


def test_strictly_rejected_strategy_can_enter_candidate_shadow_only(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    market = MarketDataStore(ledger.engine)
    store = ResearchStore(ledger.engine)
    bars = _regime_bars(count=75)
    market.insert_bars(bars, raw_object_id="TEST_RAW")
    spec = store.record_strategy_spec(
        default_strategy_spec(
            "mean_reversion",
            timeframe="1Day",
            code_sha256=research_code_sha256(),
        )
    )
    candidate_policy = PromotionGatePolicy(
        version="research_gate@0.3.0",
        minimum_oos_folds=4,
        minimum_active_oos_folds=0,
        minimum_oos_trades=0,
        minimum_candidate_count=3,
        minimum_regime_count=1,
        maximum_probability_of_backtest_overfitting=Decimal("0.20"),
        minimum_deflated_sharpe_probability=Decimal("1"),
        minimum_positive_active_oos_fold_rate=Decimal("0"),
        maximum_allowed_drawdown=Decimal("-1"),
        candidate_shadow_minimum_oos_folds=1,
        candidate_shadow_minimum_active_oos_folds=1,
        candidate_shadow_minimum_oos_trades=1,
        candidate_shadow_minimum_compounded_oos_return=Decimal("-1"),
        candidate_shadow_maximum_allowed_drawdown=Decimal("-1"),
    )
    restrictions = RestrictionRegistry.from_yaml(
        settings.restricted_securities_path
    )
    report = WalkForwardValidator(
        store,
        ledger,
        promotion_policy=candidate_policy,
        risk_policy=RiskPolicy.from_yaml(settings.risk_policy_path),
        restrictions=restrictions,
    ).run(
        symbol="AAPL",
        timeframe="1Day",
        as_of_start=bars[20].available_from,
        as_of_end=bars[-1].available_from,
        code_git_sha="test-git-sha",
        strategy_spec=spec,
        train_bars=22,
        test_bars=5,
        step_bars=5,
        embargo_bars=1,
    )
    assert report.gate_assessment["status"] == "REJECTED"
    assert report.gate_assessment["candidate_shadow"][
        "eligible_for_human_review"
    ] is True
    objects = SystemObjectStore(ledger.engine, ledger)
    objects.ensure_defaults()
    shadow = ShadowRuntime(
        ledger.engine,
        store,
        objects,
        risk_policy=RiskPolicy.from_yaml(settings.risk_policy_path),
        restrictions=restrictions,
        promotion_policy=candidate_policy,
    )
    shadow.initialize_virtual_account()

    adoption = shadow.adopt_strategy(
        strategy_spec_id=spec.strategy_spec_id,
        validation_report_id=report.validation_report_id,
        admission_tier="CANDIDATE",
        reason="Observe positive candidate evidence without Paper authority",
        approved_by="test-admin",
    )
    deployment = shadow.start_deployment(
        strategy_spec_id=spec.strategy_spec_id,
        symbol="AAPL",
        initial_cash=Decimal("100000"),
        requested_by="test-admin",
    )

    assert adoption["admission_tier"] == "CANDIDATE"
    assert deployment["admission_tier"] == "CANDIDATE"


def test_walk_forward_validation_respects_verified_history_start(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    market = MarketDataStore(ledger.engine)
    store = ResearchStore(ledger.engine)
    complete = _regime_bars(count=120)
    retained = (*complete[:20], *complete[45:])
    resumed = complete[45:]
    market.insert_bars(retained, raw_object_id="TEST_RAW")
    spec = default_strategy_spec(
        "momentum",
        timeframe="1Day",
        code_sha256=research_code_sha256(),
    )
    validator = WalkForwardValidator(store, ledger)

    with pytest.raises(DataQualityError, match="missing_intervals"):
        validator.run(
            symbol="AAPL",
            timeframe="1Day",
            as_of_start=resumed[20].available_from,
            as_of_end=resumed[-1].available_from,
            code_git_sha="test-git-sha",
            strategy_spec=spec,
            train_bars=22,
            test_bars=5,
            step_bars=5,
            embargo_bars=1,
        )

    report = validator.run(
        symbol="AAPL",
        timeframe="1Day",
        as_of_start=resumed[20].available_from,
        as_of_end=resumed[-1].available_from,
        code_git_sha="test-git-sha",
        strategy_spec=spec,
        train_bars=22,
        test_bars=5,
        step_bars=5,
        embargo_bars=1,
        history_start=resumed[0].event_time,
    )

    assert report.robustness_metrics["validation_input"]["bar_count"] == len(
        resumed
    )


def test_validation_cache_and_adoption_require_current_gate_assessment(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    market = MarketDataStore(ledger.engine)
    store = ResearchStore(ledger.engine)
    bars = _regime_bars(count=120)
    market.insert_bars(bars, raw_object_id="TEST_RAW")
    spec = store.record_strategy_spec(
        default_strategy_spec(
            "momentum",
            timeframe="1Day",
            code_sha256=research_code_sha256(),
        )
    )
    permissive = PromotionGatePolicy(
        version="research_gate@0.2.1",
        minimum_oos_folds=4,
        minimum_active_oos_folds=0,
        minimum_oos_trades=0,
        minimum_candidate_count=3,
        minimum_regime_count=1,
        maximum_probability_of_backtest_overfitting=Decimal("1"),
        minimum_deflated_sharpe_probability=Decimal("0"),
        minimum_positive_active_oos_fold_rate=Decimal("0"),
        maximum_allowed_drawdown=Decimal("-1"),
        candidate_shadow_minimum_oos_folds=1,
        candidate_shadow_minimum_active_oos_folds=1,
        candidate_shadow_minimum_oos_trades=1,
        candidate_shadow_minimum_compounded_oos_return=Decimal("-1"),
        candidate_shadow_maximum_allowed_drawdown=Decimal("-1"),
    )
    restrictions = RestrictionRegistry.from_yaml(
        settings.restricted_securities_path
    )
    objects = SystemObjectStore(ledger.engine, ledger)
    objects.ensure_defaults()
    shadow = ShadowRuntime(
        ledger.engine,
        store,
        objects,
        risk_policy=RiskPolicy.from_yaml(settings.risk_policy_path),
        restrictions=restrictions,
        promotion_policy=permissive,
    )
    shadow.initialize_virtual_account()
    handler = ResearchCoordinatorHandler.__new__(ResearchCoordinatorHandler)
    handler.settings = settings
    handler.ledger = ledger
    handler.research = store
    handler.shadow = shadow
    handler.restrictions = restrictions
    context = {
        "symbol": "AAPL",
        "timeframe": "1Day",
        "as_of": bars[-1].available_from.isoformat(),
        "strategy_spec_id": spec.strategy_spec_id,
    }
    with patch(
        "agentic_quant.coordinator_runtime.load_promotion_gate_policy",
        return_value=permissive,
    ):
        first = asyncio.run(handler._validate_strategy(context))
    assert first["eligible_for_human_review"] is True

    snapshot = PointInTimeFeatureBuilder(store).build(
        symbol="AAPL",
        timeframe="1Day",
        as_of=bars[-1].available_from,
        bars=bars,
    )
    for index in range(5):
        attempt_id = uuid7()
        store.create_generation_attempt(
            generation_attempt_id=attempt_id,
            feature_snapshot_id=snapshot.feature_snapshot_id,
            analysis_id=f"trial-analysis-{index}",
            forecast_id=f"trial-forecast-{index}",
            provider=None,
        )
        store.update_generation_attempt(
            attempt_id,
            status="ACCEPT",
            strategy_spec_id=spec.strategy_spec_id,
        )
    with pytest.raises(ValueError, match="research search count"):
        shadow.adoption_preview(
            strategy_spec_id=spec.strategy_spec_id,
            validation_report_id=str(first["validation_report_id"]),
        )
    with patch(
        "agentic_quant.coordinator_runtime.load_promotion_gate_policy",
        return_value=permissive,
    ):
        after_trials = asyncio.run(handler._validate_strategy(context))
    assert after_trials["outcome"] == "COMPLETED"
    assert after_trials["validation_report_id"] != first["validation_report_id"]

    stricter = permissive.model_copy(
        update={"version": "research_gate@0.2.2", "minimum_oos_folds": 12}
    )
    shadow._promotion_policy = stricter
    with pytest.raises(ValueError, match="promotion policy"):
        shadow.adoption_preview(
            strategy_spec_id=spec.strategy_spec_id,
            validation_report_id=str(after_trials["validation_report_id"]),
        )
    with patch(
        "agentic_quant.coordinator_runtime.load_promotion_gate_policy",
        return_value=stricter,
    ):
        after_policy = asyncio.run(handler._validate_strategy(context))
    assert after_policy["outcome"] == "COMPLETED"
    assert after_policy["eligible_for_human_review"] is False


def test_coordinator_revalidates_an_existing_accepted_spec_without_new_llm(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    market = MarketDataStore(ledger.engine)
    store = ResearchStore(ledger.engine)
    bars = _regime_bars(count=120)
    market.insert_bars(bars, raw_object_id="TEST_RAW")
    spec = store.record_strategy_spec(
        default_strategy_spec(
            "momentum",
            timeframe="1Day",
            code_sha256=research_code_sha256(),
        )
    )
    snapshot = PointInTimeFeatureBuilder(store).build(
        symbol="AAPL",
        timeframe="1Day",
        as_of=bars[-1].available_from,
        bars=bars,
    )
    attempt_id = uuid7()
    store.create_generation_attempt(
        generation_attempt_id=attempt_id,
        feature_snapshot_id=snapshot.feature_snapshot_id,
        analysis_id="accepted-analysis",
        forecast_id="accepted-forecast",
        provider="openai",
    )
    store.update_generation_attempt(
        attempt_id,
        status="ACCEPT",
        strategy_spec_id=spec.strategy_spec_id,
    )
    assert store.generated_strategy_specs(
        symbol="AAPL",
        timeframe="1Day",
        holding_period_sessions=1,
        feature_set_version=spec.feature_set_version,
        backtest_engine_version=BACKTEST_ENGINE_VERSION,
    ) == (spec,)
    permissive = DEFAULT_PROMOTION_GATE_POLICY.model_copy(
        update={
            "version": "research_gate@9.9.9",
            "minimum_oos_folds": 4,
            "minimum_active_oos_folds": 0,
            "minimum_oos_trades": 0,
            "minimum_regime_count": 1,
            "minimum_deflated_sharpe_probability": Decimal("0"),
            "minimum_positive_active_oos_fold_rate": Decimal("0"),
            "maximum_allowed_drawdown": Decimal("-1"),
        }
    )
    restrictions = RestrictionRegistry.from_yaml(
        settings.restricted_securities_path
    )
    objects = SystemObjectStore(ledger.engine, ledger)
    objects.ensure_defaults()
    shadow = ShadowRuntime(
        ledger.engine,
        store,
        objects,
        risk_policy=RiskPolicy.from_yaml(settings.risk_policy_path),
        restrictions=restrictions,
        promotion_policy=permissive,
    )
    shadow.initialize_virtual_account()
    handler = ResearchCoordinatorHandler.__new__(ResearchCoordinatorHandler)
    handler.settings = settings
    handler.ledger = ledger
    handler.research = store
    handler.shadow = shadow
    handler.restrictions = restrictions

    with patch(
        "agentic_quant.coordinator_runtime.load_promotion_gate_policy",
        return_value=permissive,
    ):
        result = asyncio.run(
            handler._validate_strategy(
                {
                    "symbol": "AAPL",
                    "timeframe": "1Day",
                    "as_of": bars[-1].available_from.isoformat(),
                    "cycle_key": "2026-09-08T20",
                    "horizon_bars": 1,
                }
            )
        )

    assert result["outcome"] == "COMPLETED"
    assert result["strategy_spec_id"] == spec.strategy_spec_id
    assert result["validation_report_id"]


def test_shadow_adoption_rejects_unsupported_minute_execution(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    store = ResearchStore(ledger.engine)
    spec = store.record_strategy_spec(
        default_strategy_spec(
            "momentum",
            timeframe="1Min",
            code_sha256=research_code_sha256(),
        )
    )
    policy = RiskPolicy.from_yaml(settings.risk_policy_path)
    restrictions = RestrictionRegistry.from_yaml(
        settings.restricted_securities_path
    )
    report_id = _seed_eligible_report(
        ledger,
        spec=spec,
        timeframe="1Min",
        risk_policy=policy,
        restrictions=restrictions,
    )
    objects = SystemObjectStore(ledger.engine, ledger)
    objects.ensure_defaults()
    shadow = ShadowRuntime(
        ledger.engine,
        store,
        objects,
        risk_policy=policy,
        restrictions=restrictions,
    )
    shadow.initialize_virtual_account()
    with pytest.raises(ValueError, match="supports 1Day strategies only"):
        shadow.adoption_preview(
            strategy_spec_id=spec.strategy_spec_id,
            validation_report_id=report_id,
        )


def test_shadow_computation_crossing_open_cannot_create_a_late_fill(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    market = MarketDataStore(ledger.engine)
    store = ResearchStore(ledger.engine)
    bars = _regime_bars(count=25)
    market.insert_bars(bars[:-2], raw_object_id="TEST_RAW")
    spec = store.record_strategy_spec(
        default_strategy_spec(
            "momentum",
            timeframe="1Day",
            code_sha256=research_code_sha256(),
        )
    )
    policy = RiskPolicy.from_yaml(settings.risk_policy_path)
    restrictions = RestrictionRegistry.from_yaml(
        settings.restricted_securities_path
    )
    report_id = _seed_eligible_report(
        ledger,
        spec=spec,
        timeframe="1Day",
        risk_policy=policy,
        restrictions=restrictions,
    )
    objects = SystemObjectStore(ledger.engine, ledger)
    objects.ensure_defaults()
    opening = MarketSessionClock("XNYS").daily_bar_session_open(
        bars[-1].event_time
    )
    clock = [opening - timedelta(seconds=1)]
    shadow = ShadowRuntime(
        ledger.engine,
        store,
        objects,
        risk_policy=policy,
        restrictions=restrictions,
        now_provider=lambda: clock[0],
    )
    shadow.initialize_virtual_account()
    shadow.adopt_strategy(
        strategy_spec_id=spec.strategy_spec_id,
        validation_report_id=report_id,
        reason="Timing regression fixture",
        approved_by="test-admin",
    )
    shadow.start_deployment(
        strategy_spec_id=spec.strategy_spec_id,
        symbol="AAPL",
        initial_cash=Decimal("100000"),
        requested_by="test-admin",
    )
    market.insert_bars((bars[-2],), raw_object_id="TEST_RAW")
    original_build = shadow.features.build

    def slow_build(**kwargs):  # type: ignore[no-untyped-def]
        snapshot = original_build(**kwargs)
        clock[0] = opening + timedelta(seconds=1)
        return snapshot

    with patch.object(shadow.features, "build", side_effect=slow_build):
        asyncio.run(
            shadow.tick(trigger="cross-open", new_exposure_paused=False)
        )
    with ledger.engine.connect() as connection:
        plans = connection.execute(select(shadow_trade_plans)).all()
    assert plans == []

    market.insert_bars((bars[-1],), raw_object_id="TEST_RAW")
    clock[0] = bars[-1].available_from + timedelta(seconds=1)
    asyncio.run(shadow.tick(trigger="next-bar", new_exposure_paused=False))
    assert not any(
        event["event_type"] == "VIRTUAL_FILL" for event in shadow.events()
    )


def test_multi_session_shadow_position_survives_ticks_and_global_pause(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    upgrade_database(settings.database_url)
    ledger = EventLedger(settings.database_url)
    market = MarketDataStore(ledger.engine)
    store = ResearchStore(ledger.engine)
    bars = _regime_bars(count=34)
    market.insert_bars(bars[:25], raw_object_id="TEST_RAW")
    base = default_strategy_spec(
        "momentum",
        timeframe="1Day",
        code_sha256=research_code_sha256(),
    )
    spec = store.record_strategy_spec(
        base.model_copy(
            update={
                "strategy_spec_id": uuid7(),
                "name": "five_session_shadow_momentum",
                "version": "0.1.0+five-session-shadow",
                "data_requirements": {
                    **base.data_requirements,
                    "holding_period": "5_sessions",
                    "holding_period_sessions": 5,
                    "position_style": "swing",
                    "paper_deployable": False,
                },
            }
        )
    )
    policy = RiskPolicy.from_yaml(settings.risk_policy_path)
    restrictions = RestrictionRegistry.from_yaml(
        settings.restricted_securities_path
    )
    report_id = _seed_eligible_report(
        ledger,
        spec=spec,
        timeframe="1Day",
        risk_policy=policy,
        restrictions=restrictions,
    )
    objects = SystemObjectStore(ledger.engine, ledger)
    objects.ensure_defaults()
    clock = [bars[25].available_from + timedelta(minutes=1)]
    shadow = ShadowRuntime(
        ledger.engine,
        store,
        objects,
        risk_policy=policy,
        restrictions=restrictions,
        now_provider=lambda: clock[0],
    )
    shadow.initialize_virtual_account()
    shadow.adopt_strategy(
        strategy_spec_id=spec.strategy_spec_id,
        validation_report_id=report_id,
        reason="Exercise the multi-session forward contract",
        approved_by="test-admin",
    )
    deployment = shadow.start_deployment(
        strategy_spec_id=spec.strategy_spec_id,
        symbol="AAPL",
        initial_cash=Decimal("100000"),
        requested_by="test-admin",
    )

    market.insert_bars((bars[25],), raw_object_id="TEST_RAW")
    asyncio.run(shadow.tick(trigger="decision", new_exposure_paused=False))
    market.insert_bars((bars[26],), raw_object_id="TEST_RAW")
    clock[0] = bars[26].available_from + timedelta(minutes=1)
    asyncio.run(shadow.tick(trigger="entry", new_exposure_paused=False))

    with ledger.engine.connect() as connection:
        opened = connection.execute(select(shadow_trade_plans)).one()
    assert opened.status == "POSITION_OPEN"
    assert Decimal(str(opened.filled_quantity)) > 0
    assert Decimal(str(opened.invalidation)) < Decimal(str(opened.entry_raw_price))
    active = shadow.deployment(str(deployment["shadow_deployment_id"]))
    assert Decimal(str(active["position_quantity"])) > 0

    market.insert_bars((bars[27],), raw_object_id="TEST_RAW")
    clock[0] = bars[27].available_from + timedelta(minutes=1)
    paused_tick = asyncio.run(
        shadow.tick(trigger="paused-exit-management", new_exposure_paused=True)
    )
    assert paused_tick["bars_processed"] == 1
    assert shadow.deployment(str(deployment["shadow_deployment_id"]))[
        "position_quantity"
    ] > 0

    for index in (28, 29, 30):
        market.insert_bars((bars[index],), raw_object_id="TEST_RAW")
        clock[0] = bars[index].available_from + timedelta(minutes=1)
        asyncio.run(
            shadow.tick(trigger=f"holding-{index}", new_exposure_paused=False)
        )

    with ledger.engine.connect() as connection:
        closed = connection.execute(select(shadow_trade_plans)).one()
    assert closed.status == "CLOSED"
    final = shadow.deployment(str(deployment["shadow_deployment_id"]))
    assert Decimal(str(final["position_quantity"])) == 0
    assert Decimal(str(shadow.virtual_account()["reserved_risk_usd"])) == 0
    exit_fill = next(
        event
        for event in shadow.events(
            deployment_id=str(deployment["shadow_deployment_id"])
        )
        if event["event_type"] == "VIRTUAL_FILL_2"
    )
    assert exit_fill["payload_json"]["exit_reason"] == "maximum_holding_period"


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
        minimum_active_oos_folds=2,
        minimum_oos_trades=2,
        minimum_candidate_count=2,
        minimum_regime_count=2,
        maximum_probability_of_backtest_overfitting=Decimal("0.25"),
        minimum_deflated_sharpe_probability=Decimal("0.90"),
        minimum_positive_active_oos_fold_rate=Decimal("0.50"),
        maximum_allowed_drawdown=Decimal("-0.20"),
        candidate_shadow_minimum_oos_folds=2,
        candidate_shadow_minimum_active_oos_folds=1,
        candidate_shadow_minimum_oos_trades=1,
        candidate_shadow_minimum_compounded_oos_return=Decimal("0"),
        candidate_shadow_maximum_allowed_drawdown=Decimal("-0.20"),
    )
    eligible = assess_research_gate(
        policy=policy,
        fold_count=8,
        active_fold_count=6,
        oos_trade_count=8,
        candidate_count=3,
        regime_count=3,
        positive_active_fold_rate=Decimal("0.75"),
        compounded_oos_return=Decimal("0.10"),
        worst_drawdown=Decimal("-0.10"),
        probability_of_backtest_overfitting=Decimal("0.10"),
        deflated_sharpe_probability=Decimal("0.97"),
    )
    insufficient = assess_research_gate(
        policy=policy,
        fold_count=2,
        active_fold_count=1,
        oos_trade_count=1,
        candidate_count=3,
        regime_count=1,
        positive_active_fold_rate=Decimal("0.75"),
        compounded_oos_return=Decimal("0.10"),
        worst_drawdown=Decimal("-0.10"),
        probability_of_backtest_overfitting=Decimal("0.10"),
        deflated_sharpe_probability=Decimal("0.97"),
    )
    assert eligible["status"] == "ELIGIBLE_FOR_HUMAN_REVIEW"
    assert eligible["automatic_promotion"] is False
    assert insufficient["status"] == "INSUFFICIENT_EVIDENCE"


def test_static_strategy_gate_treats_candidate_count_and_pbo_as_not_applicable() -> None:
    policy = PromotionGatePolicy(
        version="research_gate@0.2.0",
        minimum_oos_folds=4,
        minimum_active_oos_folds=2,
        minimum_oos_trades=2,
        minimum_candidate_count=3,
        minimum_regime_count=2,
        maximum_probability_of_backtest_overfitting=Decimal("0.20"),
        minimum_deflated_sharpe_probability=Decimal("0.90"),
        minimum_positive_active_oos_fold_rate=Decimal("0.50"),
        maximum_allowed_drawdown=Decimal("-0.20"),
        candidate_shadow_minimum_oos_folds=2,
        candidate_shadow_minimum_active_oos_folds=1,
        candidate_shadow_minimum_oos_trades=1,
        candidate_shadow_minimum_compounded_oos_return=Decimal("0"),
        candidate_shadow_maximum_allowed_drawdown=Decimal("-0.20"),
    )
    result = assess_research_gate(
        policy=policy,
        fold_count=12,
        active_fold_count=8,
        oos_trade_count=12,
        candidate_count=1,
        regime_count=3,
        positive_active_fold_rate=Decimal("0.75"),
        compounded_oos_return=Decimal("0.10"),
        worst_drawdown=Decimal("-0.10"),
        probability_of_backtest_overfitting=Decimal("0"),
        deflated_sharpe_probability=Decimal("0.97"),
        pbo_applicable=False,
        validation_subject="static_strategy",
    )

    assert result["status"] == "ELIGIBLE_FOR_HUMAN_REVIEW"
    assert result["eligible_for_human_review"] is True
    assert result["pbo_applicable"] is False
    assert result["evidence_shortfalls"] == []


def test_sparse_strategy_uses_active_folds_and_can_enter_candidate_shadow() -> None:
    policy = PromotionGatePolicy(
        version="research_gate@0.3.0",
        minimum_oos_folds=12,
        minimum_active_oos_folds=2,
        minimum_oos_trades=5,
        minimum_candidate_count=3,
        minimum_regime_count=2,
        maximum_probability_of_backtest_overfitting=Decimal("0.20"),
        minimum_deflated_sharpe_probability=Decimal("0.95"),
        minimum_positive_active_oos_fold_rate=Decimal("0.50"),
        maximum_allowed_drawdown=Decimal("-0.20"),
        candidate_shadow_minimum_oos_folds=6,
        candidate_shadow_minimum_active_oos_folds=2,
        candidate_shadow_minimum_oos_trades=5,
        candidate_shadow_minimum_compounded_oos_return=Decimal("0"),
        candidate_shadow_maximum_allowed_drawdown=Decimal("-0.20"),
    )

    result = assess_research_gate(
        policy=policy,
        fold_count=20,
        active_fold_count=2,
        oos_trade_count=5,
        candidate_count=1,
        regime_count=3,
        positive_active_fold_rate=Decimal("0.50"),
        compounded_oos_return=Decimal("0.03"),
        worst_drawdown=Decimal("-0.10"),
        probability_of_backtest_overfitting=Decimal("0"),
        deflated_sharpe_probability=Decimal("0.40"),
        pbo_applicable=False,
        validation_subject="static_strategy",
    )

    assert result["status"] == "REJECTED"
    assert result["threshold_failures"] == [
        "deflated_sharpe_probability is below policy minimum"
    ]
    assert result["candidate_shadow"]["status"] == (
        "ELIGIBLE_FOR_CANDIDATE_SHADOW_REVIEW"
    )
    assert result["candidate_shadow"]["eligible_for_human_review"] is True


def test_candidate_shadow_activity_thresholds_scale_with_holding_horizon() -> None:
    short = assess_research_gate(
        policy=DEFAULT_PROMOTION_GATE_POLICY,
        fold_count=7,
        active_fold_count=5,
        oos_trade_count=5,
        candidate_count=1,
        regime_count=2,
        positive_active_fold_rate=Decimal("0.8"),
        compounded_oos_return=Decimal("0.004"),
        worst_drawdown=Decimal("-0.01"),
        probability_of_backtest_overfitting=Decimal("0"),
        deflated_sharpe_probability=Decimal("0.4"),
        pbo_applicable=False,
        validation_subject="static_strategy",
        holding_period_sessions=1,
    )
    quarterly = assess_research_gate(
        policy=DEFAULT_PROMOTION_GATE_POLICY,
        fold_count=7,
        active_fold_count=5,
        oos_trade_count=5,
        candidate_count=1,
        regime_count=2,
        positive_active_fold_rate=Decimal("0.8"),
        compounded_oos_return=Decimal("0.004"),
        worst_drawdown=Decimal("-0.01"),
        probability_of_backtest_overfitting=Decimal("0"),
        deflated_sharpe_probability=Decimal("0.4"),
        pbo_applicable=False,
        validation_subject="static_strategy",
        holding_period_sessions=63,
    )

    assert short["candidate_shadow"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert quarterly["candidate_shadow"]["status"] == (
        "ELIGIBLE_FOR_CANDIDATE_SHADOW_REVIEW"
    )
    assert quarterly["candidate_shadow"]["activity_thresholds"] == {
        "minimum_oos_folds": 4,
        "minimum_active_oos_folds": 2,
        "minimum_oos_trades": 3,
    }
